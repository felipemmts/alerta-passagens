"""Alerta de preço de passagens aéreas (Google Flights via fast-flights 3.x).

Lê as rotas de rotas.json, busca o voo mais barato de cada uma e envia um
e-mail pelo Gmail quando o preço fica abaixo do preco_alvo (ou cai de novo
depois de um aviso). O último preço avisado fica em historico.json e o
menor preço de cada consulta fica em consultas.json.

Em rotas.json, origem, destino, ida e volta aceitam um valor ou uma lista:
todas as combinações são consultadas e vale a mais barata.
"""

import itertools
import json
import os
import smtplib
import ssl
import statistics
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARQ_ROTAS = BASE / "rotas.json"
ARQ_HISTORICO = BASE / "historico.json"
ARQ_CONSULTAS = BASE / "consultas.json"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORTA = 465

DIAS_CONSULTAS = 90  # quanto tempo de consultas guardar em consultas.json
PAUSA_SEGUNDOS = float(os.environ.get("PAUSA_SEGUNDOS", "3"))  # entre buscas no Google


# --------------------------------------------------------------------------
# Arquivos
# --------------------------------------------------------------------------

def carregar_json(caminho, padrao):
    if not caminho.exists():
        return padrao
    texto = caminho.read_text(encoding="utf-8").strip()
    return json.loads(texto) if texto else padrao


def salvar_json(caminho, dados):
    caminho.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _lista(valor):
    if valor is None:
        return [None]
    return list(valor) if isinstance(valor, (list, tuple)) else [valor]


def _juntar(valor):
    return "/".join(str(v) for v in _lista(valor)) if valor else ""


def chave_rota(rota):
    if rota.get("nome"):
        return rota["nome"]
    ida = _juntar(rota.get("ida")) or f"{rota['ida_de']}_a_{rota['ida_ate']}"
    if rota.get("estadia_dias"):
        volta = f"{_juntar(rota['estadia_dias'])}d"
    else:
        volta = _juntar(rota.get("volta")) or "so-ida"
    return (f"{_juntar(rota['origem'])}-{_juntar(rota['destino'])}-"
            f"{ida}-{volta}-{rota.get('adultos', 1)}ad")


def _datas_ida(rota):
    """Datas de ida: "ida" (valor ou lista) ou o intervalo "ida_de".."ida_ate"."""
    if rota.get("ida_de"):
        dia, fim = date.fromisoformat(rota["ida_de"]), date.fromisoformat(rota["ida_ate"])
        datas = []
        while dia <= fim:
            datas.append(dia.isoformat())
            dia += timedelta(days=1)
        return datas
    return _lista(rota["ida"])


def _pares_ida_volta(rota):
    """Pares (ida, volta). Com "estadia_dias", a volta é ida + estadia."""
    idas = _datas_ida(rota)
    if rota.get("estadia_dias"):
        return [
            (ida, (date.fromisoformat(ida) + timedelta(days=int(n))).isoformat())
            for ida in idas for n in _lista(rota["estadia_dias"])
        ]
    return list(itertools.product(idas, _lista(rota.get("volta"))))


def expandir(rota, hoje=None):
    """Lista as combinações (origem, destino, ida, volta) com ida a partir de hoje."""
    hoje = hoje or date.today()
    combos = []
    for o, d, (ida, volta) in itertools.product(
        _lista(rota["origem"]), _lista(rota["destino"]), _pares_ida_volta(rota)
    ):
        if date.fromisoformat(ida) < hoje:
            continue
        if volta and date.fromisoformat(volta) <= date.fromisoformat(ida):
            continue
        combos.append({**rota, "origem": o, "destino": d, "ida": ida, "volta": volta})
    return combos


# --------------------------------------------------------------------------
# Regras
# --------------------------------------------------------------------------

def rota_expirada(rota, hoje=None):
    return not expandir(rota, hoje)


def decidir_aviso(preco, preco_alvo, ultimo_avisado):
    """Retorna (avisar, novo_ultimo_avisado).

    - Preço >= alvo: não avisa e limpa o histórico, para que uma nova queda
      abaixo do alvo gere aviso de novo.
    - Preço < alvo e nunca avisado: avisa.
    - Preço < alvo e menor que o último avisado: avisa (caiu de novo).
    - Caso contrário: não avisa e mantém o histórico.
    """
    if preco >= preco_alvo:
        return False, None
    if ultimo_avisado is None or preco < ultimo_avisado:
        return True, preco
    return False, ultimo_avisado


# --------------------------------------------------------------------------
# Busca (fast-flights 3.x)
# --------------------------------------------------------------------------

def montar_query(rota, moeda):
    from fast_flights import FlightQuery, Passengers, create_query

    trechos = [
        FlightQuery(date=rota["ida"], from_airport=rota["origem"], to_airport=rota["destino"])
    ]
    if rota.get("volta"):
        trechos.append(
            FlightQuery(date=rota["volta"], from_airport=rota["destino"], to_airport=rota["origem"])
        )
    return create_query(
        flights=trechos,
        trip="round-trip" if rota.get("volta") else "one-way",
        passengers=Passengers(adults=int(rota.get("adultos", 1))),
        currency=moeda,
        language="pt-BR",
        max_stops=rota.get("max_escalas"),
    )


def _pegar(dados, *indices):
    """Acessa dados[i][j]... devolvendo None se algum nível faltar."""
    for i in indices:
        try:
            dados = dados[i]
        except (IndexError, KeyError, TypeError):
            return None
    return dados


def parse_tolerante(html):
    """Parser alternativo ao da fast-flights 3.1.0.

    O parser da biblioteca quebra quando o bloco "outros voos" (payload[3])
    vem vazio e ignora o bloco "melhores voos" (payload[2]). Este lê os dois
    e pula itens incompletos.
    """
    from fast_flights.model import Airport, Flights, SimpleDatetime, SingleFlight
    from selectolax.lexbor import LexborHTMLParser

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("página do Google sem dados de voos (bloqueio ou consentimento?)")
    js = script.text()
    if "data:" not in js:
        raise RuntimeError("formato inesperado da página do Google")
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise RuntimeError("Google não encontrou voos para esta busca")
    payload = json.loads(data)

    def hora(v):
        v = [*(v or []), None, None]
        return (v[0] or 0, v[1] or 0)

    resultados = []
    for bloco in (_pegar(payload, 2, 0), _pegar(payload, 3, 0)):
        for item in bloco or []:
            voo, preco = _pegar(item, 0), _pegar(item, 1, 0, 1)
            if not voo or not preco:
                continue
            trechos = []
            for t in _pegar(voo, 2) or []:
                trechos.append(SingleFlight(
                    from_airport=Airport(code=_pegar(t, 3), name=_pegar(t, 4)),
                    to_airport=Airport(code=_pegar(t, 6), name=_pegar(t, 5)),
                    departure=SimpleDatetime(date=tuple(_pegar(t, 20) or ()), time=hora(_pegar(t, 8))),
                    arrival=SimpleDatetime(date=tuple(_pegar(t, 21) or ()), time=hora(_pegar(t, 10))),
                    duration=_pegar(t, 11),
                    plane_type=_pegar(t, 17),
                ))
            resultados.append(Flights(
                type=_pegar(voo, 0), price=preco, airlines=_pegar(voo, 1) or [],
                flights=trechos, carbon=None,
            ))
    return resultados


def buscar_mais_barato(query):
    """Retorna o resultado mais barato (objeto com .price/.airlines/.flights)."""
    from fast_flights import fetch_flights_html
    from fast_flights.parser import parse

    html = fetch_flights_html(query)  # mesma requisição feita por get_flights(query)
    try:
        resultados = list(parse(html))
    except Exception as e:
        print(f"  parser da fast-flights falhou ({type(e).__name__}: {e}); usando parser tolerante")
        resultados = []
    if not resultados:
        resultados = parse_tolerante(html)

    resultados = [r for r in resultados if getattr(r, "price", 0)]
    if not resultados:
        raise RuntimeError("nenhum voo com preço encontrado")
    return min(resultados, key=lambda r: r.price)


# --------------------------------------------------------------------------
# Formatação e e-mail
# --------------------------------------------------------------------------

def _fmt_dt(dt):
    """SimpleDatetime -> 'dd/mm/aaaa hh:mm'."""
    try:
        a, m, d = dt.date
        h, mi = dt.time
        return f"{d:02d}/{m:02d}/{a:04d} {h:02d}:{mi:02d}"
    except (AttributeError, TypeError, ValueError):
        return "?"


def resumir_voo(voo):
    trechos = list(getattr(voo, "flights", None) or [])
    escalas = max(len(trechos) - 1, 0)
    companhias = ", ".join(getattr(voo, "airlines", None) or []) or "?"
    if trechos:
        partida = _fmt_dt(trechos[0].departure)
        chegada = _fmt_dt(trechos[-1].arrival)
        conexoes = [t.to_airport.code for t in trechos[:-1]]
    else:
        partida = chegada = "?"
        conexoes = []
    return {
        "companhia": companhias,
        "partida": partida,
        "chegada": chegada,
        "escalas": escalas,
        "conexoes": conexoes,
    }


def estatisticas(consultas):
    precos = [c["preco"] for c in consultas]
    if not precos:
        return None
    return {"n": len(precos), "min": min(precos), "mediana": int(statistics.median(precos))}


def montar_email(rota, combo, moeda, preco, resumo, link, ultimo_avisado, stats=None):
    trajeto = f"{combo['origem']} → {combo['destino']}"
    excelente = rota.get("preco_excelente")
    selo = "🔥 PREÇO EXCELENTE" if excelente and preco <= excelente else "✈️"
    titulo = f"{rota['nome']}: " if rota.get("nome") else ""
    assunto = f"{selo} {titulo}{trajeto} {preco} {moeda} (alvo {rota['preco_alvo']} {moeda})"

    datas = f"Ida {combo['ida']}" + (f" · Volta {combo['volta']}" if combo.get("volta") else " (só ida)")
    escalas = "direto" if resumo["escalas"] == 0 else (
        f"{resumo['escalas']} ({', '.join(resumo['conexoes'])})"
    )
    faixa = f"{rota['preco_alvo']} {moeda}"
    if excelente:
        faixa += f" (excelente: até {excelente} {moeda})"
    linhas = [
        f"Passagem abaixo do preço-alvo: {trajeto}",
        "",
        f"Preço:      {preco} {moeda} ({rota.get('adultos', 1)} adulto(s))",
        f"Preço-alvo: {faixa}",
    ]
    if ultimo_avisado is not None:
        linhas.append(f"Último aviso: {ultimo_avisado} {moeda} (o preço caiu de novo)")
    if stats:
        linhas.append(
            f"Histórico:  mínimo {stats['min']} / mediana {stats['mediana']} {moeda} "
            f"em {stats['n']} consulta(s) dos últimos {DIAS_CONSULTAS} dias"
        )
    linhas += [
        f"Datas:      {datas}",
        f"Companhia:  {resumo['companhia']}",
        f"Partida (ida): {resumo['partida']}",
        f"Chegada (ida): {resumo['chegada']}",
        f"Escalas:    {escalas}",
        "",
        f"Google Flights: {link}",
    ]
    return assunto, "\n".join(linhas)


def enviar_email(assunto, corpo):
    remetente = os.environ["EMAIL_REMETENTE"]
    senha = os.environ["EMAIL_SENHA"]
    destino = os.environ["EMAIL_DESTINO"]

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = destino
    msg.set_content(corpo)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORTA, context=ssl.create_default_context()) as smtp:
        smtp.login(remetente, senha)
        smtp.send_message(msg)


# --------------------------------------------------------------------------
# Execução
# --------------------------------------------------------------------------

def descrever(combo):
    volta = f" volta {combo['volta']}" if combo.get("volta") else ""
    return f"{combo['origem']}→{combo['destino']} ida {combo['ida']}{volta}"


def buscar_combinacoes(chave, combos, moeda, buscar, montar):
    """Busca cada combinação e devolve (combo, query, voo) da mais barata.

    Falhas individuais são registradas e ignoradas; só lança exceção se
    todas as combinações falharem.
    """
    melhor, erros = None, []
    for i, combo in enumerate(combos):
        if i and PAUSA_SEGUNDOS:
            time.sleep(PAUSA_SEGUNDOS)  # evita bombardear o Google
        try:
            query = montar(combo, moeda)
            voo = buscar(query)
        except Exception as e:
            erros.append(e)
            print(f"[{chave}]   {descrever(combo)}: ERRO {type(e).__name__}: {e}", file=sys.stderr)
            continue
        print(f"[{chave}]   {descrever(combo)}: {int(voo.price)} {moeda}")
        if melhor is None or voo.price < melhor[2].price:
            melhor = (combo, query, voo)
    if melhor is None:
        raise erros[-1]
    if erros:
        print(f"[{chave}]   {len(erros)} de {len(combos)} busca(s) falharam")
    return melhor


def processar_rota(rota, moeda, historico, buscar=None, enviar=None, montar=None,
                   consultas=None, hoje=None):
    """Processa uma rota. Retorna True se avisou. Lança exceção em falha."""
    buscar = buscar or buscar_mais_barato
    enviar = enviar or enviar_email
    montar = montar or montar_query
    consultas = consultas if consultas is not None else {}

    chave = chave_rota(rota)
    combo, query, voo = buscar_combinacoes(chave, expandir(rota, hoje), moeda, buscar, montar)
    preco = int(voo.price)

    registros = consultas.setdefault(chave, [])
    registros.append({
        "em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preco": preco,
        "origem": combo["origem"],
        "destino": combo["destino"],
        "ida": combo["ida"],
        "volta": combo.get("volta"),
    })

    anterior = historico.get(chave, {}).get("ultimo_preco_avisado")
    avisar, novo = decidir_aviso(preco, rota["preco_alvo"], anterior)
    print(f"[{chave}] menor preço: {preco} {moeda} em {descrever(combo)} "
          f"(alvo {rota['preco_alvo']}, último aviso {anterior})")

    if avisar:
        assunto, corpo = montar_email(rota, combo, moeda, preco, resumir_voo(voo), query.url(),
                                      anterior, estatisticas(registros))
        enviar(assunto, corpo)
        print(f"[{chave}] e-mail enviado.")

    if novo is None:
        historico.pop(chave, None)
    elif avisar:
        historico[chave] = {
            "ultimo_preco_avisado": novo,
            "moeda": moeda,
            "avisado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    return avisar


def main(buscar=None, enviar=None, montar=None, hoje=None):
    config = carregar_json(ARQ_ROTAS, {})
    moeda = config.get("moeda", "EUR")
    rotas = config.get("rotas", [])
    historico = carregar_json(ARQ_HISTORICO, {})
    consultas = carregar_json(ARQ_CONSULTAS, {})

    ativas = [r for r in rotas if not rota_expirada(r, hoje)]
    for r in rotas:
        if r not in ativas:
            print(f"[{chave_rota(r)}] ignorada: data de ida já passou.")
            historico.pop(chave_rota(r), None)

    def salvar():
        salvar_json(ARQ_HISTORICO, historico)
        limite = (datetime.now(timezone.utc) - timedelta(days=DIAS_CONSULTAS)).isoformat()
        for chave in list(consultas):
            consultas[chave] = [c for c in consultas[chave] if c["em"] >= limite]
            if not consultas[chave]:
                del consultas[chave]
        salvar_json(ARQ_CONSULTAS, consultas)

    if not ativas:
        print("Nenhuma rota ativa.")
        salvar()
        return 0

    falhas = 0
    for rota in ativas:
        try:
            processar_rota(rota, moeda, historico, buscar, enviar, montar, consultas, hoje)
        except Exception as e:  # uma rota com erro não derruba as outras
            falhas += 1
            print(f"[{chave_rota(rota)}] ERRO: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc()

    salvar()

    if falhas == len(ativas):
        print("Todas as rotas falharam.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
