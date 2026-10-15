"""Alerta de preço de passagens aéreas (Google Flights via fast-flights 3.x).

Lê as rotas de rotas.json, busca o voo mais barato de cada uma e envia um
e-mail pelo Gmail quando o preço fica abaixo do preco_alvo (ou cai de novo
depois de um aviso). O último preço avisado fica em historico.json e o
menor preço de cada consulta fica em consultas.json.

Em rotas.json, origem, destino, ida e volta aceitam um valor ou uma lista:
todas as combinações são consultadas e vale a mais barata.
"""

import base64
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
PASTA_DEBUG = BASE / "debug"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORTA = 465

DIAS_CONSULTAS = 90  # quanto tempo de consultas guardar em consultas.json
PAUSA_SEGUNDOS = float(os.environ.get("PAUSA_SEGUNDOS", "3"))  # entre buscas no Google
TIMEOUT_SEGUNDOS = 40  # por requisição ao Google: se travar, desiste e segue
TENTATIVAS_EMAIL = 3


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
    elif rota.get("volta_de"):
        volta = f"{rota['volta_de']}_a_{rota['volta_ate']}"
    else:
        volta = _juntar(rota.get("volta")) or "so-ida"
    return (f"{_juntar(rota['origem'])}-{_juntar(rota['destino'])}-"
            f"{ida}-{volta}-{rota.get('adultos', 1)}ad")


def _intervalo(de, ate):
    dia, fim = date.fromisoformat(de), date.fromisoformat(ate)
    datas = []
    while dia <= fim:
        datas.append(dia.isoformat())
        dia += timedelta(days=1)
    return datas


def _datas_ida(rota):
    """Datas de ida: "ida" (valor ou lista) ou o intervalo "ida_de".."ida_ate"."""
    if rota.get("ida_de"):
        return _intervalo(rota["ida_de"], rota["ida_ate"])
    return _lista(rota["ida"])


def _pares_ida_volta(rota):
    """Pares (ida, volta).

    Com "estadia_dias", a volta é ida + estadia; com "volta_de".."volta_ate",
    todas as combinações de ida e volta do intervalo; senão, "volta".
    """
    idas = _datas_ida(rota)
    if rota.get("estadia_dias"):
        return [
            (ida, (date.fromisoformat(ida) + timedelta(days=int(n))).isoformat())
            for ida in idas for n in _lista(rota["estadia_dias"])
        ]
    if rota.get("volta_de"):
        return list(itertools.product(idas, _intervalo(rota["volta_de"], rota["volta_ate"])))
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
    query = create_query(
        flights=trechos,
        trip="round-trip" if rota.get("volta") else "one-way",
        passengers=Passengers(adults=int(rota.get("adultos", 1))),
        currency=moeda,
        language="pt-BR",
        max_stops=rota.get("max_escalas"),
    )
    query._combo = rota  # usado na tentativa alternativa de buscar_mais_barato
    return query


def _pegar(dados, *indices):
    """Acessa dados[i][j]... devolvendo None se algum nível faltar."""
    for i in indices:
        try:
            dados = dados[i]
        except (IndexError, KeyError, TypeError):
            return None
    return dados


def _eh_trecho(t):
    return (isinstance(t, list) and len(t) > 6
            and isinstance(t[3], str) and len(t[3]) == 3
            and isinstance(t[6], str) and len(t[6]) == 3)


def _eh_item_voo(x):
    """[voo, [[_, preço], ...]] com voo[2] = lista de trechos."""
    voo, preco = _pegar(x, 0), _pegar(x, 1, 0, 1)
    trechos = _pegar(voo, 2)
    return (isinstance(preco, int) and preco > 0 and isinstance(trechos, list)
            and trechos and all(_eh_trecho(t) for t in trechos))


def _procurar_itens(dados, profundidade=0):
    if not isinstance(dados, list) or profundidade > 12:
        return
    if _eh_item_voo(dados):
        yield dados
        return
    for x in dados:
        yield from _procurar_itens(x, profundidade + 1)


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

    blocos = [b for b in (_pegar(payload, 2, 0), _pegar(payload, 3, 0)) if b]
    if not blocos:
        # Estrutura diferente da esperada: procura itens de voo em toda a página.
        blocos = [list(_procurar_itens(payload))]

    resultados = []
    for bloco in blocos:
        for item in bloco or []:
            voo, preco = _pegar(item, 0), _pegar(item, 1, 0, 1)
            if not voo or not isinstance(preco, int) or not preco:
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


# Formato completo usado pelas URLs do próprio Google Flights. A fast-flights
# 3.1.0 omite o cabeçalho (campos 1 e 2) e o tipo de aeroporto (campo 1 dentro
# de 13/14); quando a busca dela não traz voos, tentamos este formato.

def _varint(n):
    n &= (1 << 64) - 1
    saida = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            saida.append(b | 0x80)
        else:
            saida.append(b)
            return bytes(saida)


def _pb_int(campo, valor):
    return _varint(campo << 3) + _varint(valor)


def _pb_bytes(campo, dados):
    if isinstance(dados, str):
        dados = dados.encode()
    return _varint((campo << 3) | 2) + _varint(len(dados)) + dados


def tfs_formato_google(combo):
    trechos = [(combo["ida"], combo["origem"], combo["destino"])]
    if combo.get("volta"):
        trechos.append((combo["volta"], combo["destino"], combo["origem"]))
    msg = _pb_int(1, 28) + _pb_int(2, 2)
    for dia, de, para in trechos:
        trecho = (_pb_bytes(2, dia)
                  + _pb_bytes(13, _pb_int(1, 1) + _pb_bytes(2, de))
                  + _pb_bytes(14, _pb_int(1, 1) + _pb_bytes(2, para)))
        if combo.get("max_escalas") is not None:
            trecho += _pb_int(5, int(combo["max_escalas"]))
        msg += _pb_bytes(3, trecho)
    msg += _pb_int(8, 1) * int(combo.get("adultos", 1))  # adultos
    msg += _pb_int(9, 1)                                  # econômica
    msg += _pb_int(14, 1)
    msg += _pb_bytes(16, _pb_int(1, -1))
    msg += _pb_int(19, 1 if combo.get("volta") else 2)   # ida e volta / só ida
    return base64.urlsafe_b64encode(msg).decode().rstrip("=")


def link_busca(combo, moeda):
    """Link do Google Flights que abre direto nesta busca."""
    return (f"https://www.google.com/travel/flights/search?tfs={tfs_formato_google(combo)}"
            f"&hl=pt-BR&curr={moeda}")


def _baixar(params):
    from fast_flights.fetcher import URL
    from primp import Client

    cliente = Client(impersonate="chrome_145", impersonate_os="macos", referer=True,
                     cookie_store=True, timeout=TIMEOUT_SEGUNDOS)
    return cliente.get(URL, params=params).text


def _extrair(html):
    """Lista de voos com preço a partir do HTML (lança RuntimeError se nenhum)."""
    from fast_flights.parser import parse

    try:
        resultados = list(parse(html))
    except Exception as e:
        resultados = []  # parser da biblioteca falhou: tenta o tolerante
    if not resultados:
        resultados = parse_tolerante(html)
    com_preco = [r for r in resultados if getattr(r, "price", 0)]
    if not com_preco:
        raise RuntimeError(f"nenhum voo com preço encontrado ({len(resultados)} resultado(s) sem preço)")
    return com_preco


def _diagnosticar(html, nome):
    """Salva a página da última tentativa em debug/ (vira artefato do workflow)."""
    PASTA_DEBUG.mkdir(exist_ok=True)
    (PASTA_DEBUG / f"{nome}.html").write_text(html, encoding="utf-8")


def buscar_mais_barato(query):
    """Retorna o resultado mais barato (objeto com .price/.airlines/.flights)."""
    combo = getattr(query, "_combo", None)
    tentativas = [("fast-flights", query.params())]
    if combo:
        google = {"tfs": tfs_formato_google(combo), "hl": query.language, "curr": query.currency}
        tentativas.append(("formato Google", google))
        # Algumas datas falham numa execução e funcionam na seguinte: mais uma
        # tentativa depois de uma pausa maior.
        tentativas.append(("formato Google, 2ª tentativa", google))

    ultimo_erro, html = None, None
    for i, (nome, params) in enumerate(tentativas):
        if i and PAUSA_SEGUNDOS:
            time.sleep(PAUSA_SEGUNDOS * (4 if i == 2 else 1))
        try:
            html = _baixar(params)
            resultados = _extrair(html)
        except Exception as e:
            ultimo_erro = e
            continue
        return min(resultados, key=lambda r: r.price)
    if combo and html:
        _diagnosticar(html, descrever(combo).replace(" ", "_").replace("→", "-"))
    raise ultimo_erro


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


def _fmt_data(iso):
    a, m, d = iso.split("-")
    return f"{d}/{m}/{a}"


def montar_email(rota, combo, moeda, preco, resumo, link, ultimo_avisado, stats=None,
                 opcoes=None, modo_resumo=False):
    """Devolve (assunto, texto, html). opcoes = [(combo, voo, link), ...] mais baratas."""
    trajeto = f"{combo['origem']} → {combo['destino']}"
    excelente = rota.get("preco_excelente")
    titulo = f"{rota['nome']}: " if rota.get("nome") else ""
    if modo_resumo:
        selo = "📊 Resumo"
    else:
        selo = "🔥 PREÇO EXCELENTE" if excelente and preco <= excelente else "✈️"
    assunto = f"{selo} {titulo}{trajeto} {preco} {moeda} (alvo {rota['preco_alvo']} {moeda})"

    datas = f"Ida {_fmt_data(combo['ida'])}" + (
        f" · Volta {_fmt_data(combo['volta'])}" if combo.get("volta") else " (só ida)")
    escalas = "direto" if resumo["escalas"] == 0 else (
        f"{resumo['escalas']} ({', '.join(resumo['conexoes'])})"
    )
    faixa = f"{rota['preco_alvo']} {moeda}"
    if excelente:
        faixa += f" (excelente: até {excelente} {moeda})"
    if modo_resumo:
        chamada = (f"Resumo da busca: o melhor preço agora é {preco} {moeda}"
                   + (" (abaixo do alvo)." if preco < rota["preco_alvo"] else " (ainda não está abaixo do alvo)."))
    else:
        chamada = f"Passagem abaixo do preço-alvo: {trajeto}"

    detalhes = [
        ("Preço", f"{preco} {moeda} ({rota.get('adultos', 1)} adulto(s))"),
        ("Preço-alvo", faixa),
    ]
    if ultimo_avisado is not None and not modo_resumo:
        detalhes.append(("Último aviso", f"{ultimo_avisado} {moeda} (o preço caiu de novo)"))
    if stats:
        detalhes.append(("Histórico", f"mínimo {stats['min']} / mediana {stats['mediana']} {moeda} "
                                       f"em {stats['n']} consulta(s) dos últimos {DIAS_CONSULTAS} dias"))
    detalhes += [
        ("Datas", datas),
        ("Companhia", resumo["companhia"]),
        ("Partida (ida)", resumo["partida"]),
        ("Chegada (ida)", resumo["chegada"]),
        ("Escalas", escalas),
    ]
    opcoes = opcoes or []

    # texto puro
    linhas = [chamada, "", f"Abrir esta busca no Google Flights: {link}", ""]
    linhas += [f"{k + ':':<15}{v}" for k, v in detalhes]
    if len(opcoes) > 1:
        linhas += ["", "Outras datas mais baratas:"]
        for c, v, l in opcoes:
            volta = f" → {_fmt_data(c['volta'])}" if c.get("volta") else ""
            linhas.append(f"  {_fmt_data(c['ida'])}{volta}: {int(v.price)} {moeda}  {l}")
    texto = "\n".join(linhas)

    # html
    from html import escape as e
    botao = ('display:inline-block;background:#1a73e8;color:#fff;text-decoration:none;'
             'padding:12px 20px;border-radius:6px;font-weight:bold')
    linhas_det = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#5f6368">{e(k)}</td>'
        f'<td style="padding:4px 0"><b>{e(v)}</b></td></tr>' for k, v in detalhes)
    tabela_op = ""
    if len(opcoes) > 1:
        lin = "".join(
            f'<tr><td style="padding:6px 12px 6px 0">{_fmt_data(c["ida"])}'
            f'{" → " + _fmt_data(c["volta"]) if c.get("volta") else ""}</td>'
            f'<td style="padding:6px 12px 6px 0"><b>{int(v.price)} {e(moeda)}</b></td>'
            f'<td><a href="{e(l)}">ver voos</a></td></tr>' for c, v, l in opcoes)
        tabela_op = ('<h3 style="margin:24px 0 8px">Datas mais baratas</h3>'
                     f'<table style="border-collapse:collapse">{lin}</table>')
    html = (f'<div style="font-family:Arial,sans-serif;font-size:15px;color:#202124">'
            f'<p style="font-size:17px">{e(chamada)}</p>'
            f'<p><a href="{e(link)}" style="{botao}">Abrir no Google Flights</a></p>'
            f'<table style="border-collapse:collapse">{linhas_det}</table>'
            f'{tabela_op}</div>')
    return assunto, texto, html


def enviar_email(assunto, corpo, html=None):
    remetente = os.environ["EMAIL_REMETENTE"].strip()
    # A senha de app do Gmail aparece com espaços ("abcd efgh ijkl mnop") e o
    # Secret pode ter sido colado com quebra de linha: remove tudo isso.
    senha = "".join(os.environ["EMAIL_SENHA"].split())
    destino = os.environ["EMAIL_DESTINO"].strip()
    # Diagnóstico sem expor as credenciais.
    print(f"  e-mail: remetente @{remetente.rpartition('@')[2] or '(sem @!)'}, "
          f"senha com {len(senha)} caracteres"
          f"{'' if senha.isascii() and senha.isalpha() else ' (não são só letras: senha de app tem só letras)'}, "
          f"destino @{destino.rpartition('@')[2] or '(sem @!)'}")

    if len(senha) != 16:
        raise RuntimeError(
            f"EMAIL_SENHA tem {len(senha)} caracteres, mas a senha de app do Gmail tem 16 letras: "
            "gere uma nova em https://myaccount.google.com/apppasswords e atualize o Secret"
        )

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = destino
    msg.set_content(corpo)
    if html:
        msg.add_alternative(html, subtype="html")

    # Falhas de conexão costumam ser passageiras: tenta de novo antes de desistir.
    for tentativa in range(1, TENTATIVAS_EMAIL + 1):
        try:
            _enviar_smtp(remetente, senha, msg)
            return
        except RuntimeError:
            raise  # login recusado: tentar de novo não resolve
        except (smtplib.SMTPException, OSError) as e:
            if tentativa == TENTATIVAS_EMAIL:
                raise
            print(f"  envio do e-mail falhou ({type(e).__name__}); nova tentativa em 15 s",
                  file=sys.stderr)
            time.sleep(15)


def _enviar_smtp(remetente, senha, msg):
    contexto = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORTA, context=contexto, timeout=30) as smtp:
            smtp.login(remetente, senha)
            smtp.send_message(msg)
            return
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            f"Gmail recusou o login ({e.smtp_code}): confira EMAIL_REMETENTE e gere uma nova "
            "senha de app em https://myaccount.google.com/apppasswords"
        ) from e
    except (smtplib.SMTPServerDisconnected, OSError) as e:
        print(f"  porta {SMTP_PORTA} (SSL) falhou: {type(e).__name__}: {e}; tentando 587 (STARTTLS)",
              file=sys.stderr)

    try:
        with smtplib.SMTP(SMTP_HOST, 587, timeout=30) as smtp:
            smtp.starttls(context=contexto)
            smtp.login(remetente, senha)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            f"Gmail recusou o login ({e.smtp_code}): confira EMAIL_REMETENTE e gere uma nova "
            "senha de app em https://myaccount.google.com/apppasswords"
        ) from e


# --------------------------------------------------------------------------
# Execução
# --------------------------------------------------------------------------

def descrever(combo):
    volta = f" volta {combo['volta']}" if combo.get("volta") else ""
    return f"{combo['origem']}→{combo['destino']} ida {combo['ida']}{volta}"


def buscar_combinacoes(chave, combos, moeda, buscar, montar):
    """Busca cada combinação e devolve (combo, query, voo, todos) da mais barata.

    Falhas individuais são registradas e ignoradas; só lança exceção se
    todas as combinações falharem.
    """
    melhor, pendentes, erro, todos = None, list(combos), None, []
    # O Google falha de forma passageira em algumas datas: as que falharem na
    # primeira rodada são tentadas de novo numa segunda, depois de uma pausa.
    for rodada in (1, 2):
        falharam = []
        if rodada == 2 and pendentes and PAUSA_SEGUNDOS:
            time.sleep(PAUSA_SEGUNDOS * 5)
        for i, combo in enumerate(pendentes):
            if i and PAUSA_SEGUNDOS:
                time.sleep(PAUSA_SEGUNDOS)  # evita bombardear o Google
            try:
                query = montar(combo, moeda)
                voo = buscar(query)
            except Exception as e:
                falharam.append(combo)
                erro = e
                continue
            print(f"[{chave}]   {descrever(combo)}: {int(voo.price)} {moeda}")
            todos.append((combo, voo))
            if melhor is None or voo.price < melhor[2].price:
                melhor = (combo, query, voo)
        pendentes = falharam
        if not pendentes:
            break
    for combo in pendentes:
        print(f"[{chave}]   {descrever(combo)}: sem resposta do Google ({erro})")
    if melhor is None:
        raise erro
    todos.sort(key=lambda cv: cv[1].price)
    return (*melhor, todos)


def processar_rota(rota, moeda, historico, buscar=None, enviar=None, montar=None,
                   consultas=None, hoje=None, modo_resumo=False):
    """Processa uma rota. Retorna True se avisou. Lança exceção em falha."""
    buscar = buscar or buscar_mais_barato
    enviar = enviar or enviar_email
    montar = montar or montar_query
    consultas = consultas if consultas is not None else {}

    chave = chave_rota(rota)
    combo, query, voo, todos = buscar_combinacoes(chave, expandir(rota, hoje), moeda, buscar, montar)
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

    if modo_resumo:
        avisar, novo = True, anterior  # manda o e-mail, mas não mexe no histórico de avisos
    if avisar:
        opcoes = [(c, v, link_busca(c, moeda)) for c, v in todos[:5]]
        assunto, texto, html = montar_email(
            rota, combo, moeda, preco, resumir_voo(voo), link_busca(combo, moeda),
            anterior, estatisticas(registros), opcoes, modo_resumo)
        enviar(assunto, texto, html)
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


def main(buscar=None, enviar=None, montar=None, hoje=None, modo_resumo=False):
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
            processar_rota(rota, moeda, historico, buscar, enviar, montar, consultas, hoje,
                           modo_resumo)
        except Exception as e:  # uma rota com erro não derruba as outras
            falhas += 1
            print(f"[{chave_rota(rota)}] ERRO: {e}", file=sys.stderr)
            if not isinstance(e, RuntimeError):  # erro inesperado: mostra detalhes
                traceback.print_exc()

    salvar()

    if falhas == len(ativas):
        print("Todas as rotas falharam.", file=sys.stderr)
        return 1
    return 0


def teste_email():
    """Envia um e-mail de teste para conferir os Secrets."""
    try:
        enviar_email("✈️ Alerta de passagens: e-mail de teste",
                     "Se você recebeu esta mensagem, o envio de alertas está funcionando.")
    except Exception as e:
        print(f"Falha no e-mail de teste: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("E-mail de teste enviado.")
    return 0


if __name__ == "__main__":
    if os.environ.get("TESTE_EMAIL", "").lower() == "true":
        sys.exit(teste_email())
    sys.exit(main(modo_resumo=os.environ.get("RESUMO", "").lower() == "true"))
