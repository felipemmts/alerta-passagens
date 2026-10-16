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
import re
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
# Os arquivos podem ser trocados por variáveis de ambiente (útil para ter mais de um alerta).
ARQ_ROTAS = BASE / os.environ.get("ARQUIVO_ROTAS", "rotas.json")
ARQ_HISTORICO = BASE / os.environ.get("ARQUIVO_HISTORICO", "historico.json")
ARQ_CONSULTAS = BASE / os.environ.get("ARQUIVO_CONSULTAS", "consultas.json")
PASTA_DEBUG = BASE / os.environ.get("PASTA_DEBUG", "debug")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORTA = 465

DIAS_CONSULTAS = 90  # quanto tempo de consultas guardar em consultas.json
PAUSA_SEGUNDOS = float(os.environ.get("PAUSA_SEGUNDOS", "3"))  # entre buscas no Google
TIMEOUT_SEGUNDOS = 40  # por requisição ao Google: se travar, desiste e segue
TENTATIVAS_EMAIL = 3
# Prazo da execução: perto dele, para de buscar e envia o que já encontrou
# (o GitHub mata a Action no timeout e aí tudo se perderia).
PRAZO_MINUTOS = float(os.environ.get("PRAZO_MINUTOS", "0") or 0)
_INICIO = time.monotonic()


def segundos_restantes():
    if not PRAZO_MINUTOS:
        return float("inf")
    return PRAZO_MINUTOS * 60 - (time.monotonic() - _INICIO)
# Plano C para rotas em que o Google só manda os voos depois de carregar a página.
USAR_NAVEGADOR = os.environ.get("USAR_NAVEGADOR", "").lower() == "true"
_navegador = {"direto": set(), "falhas_seguidas": 0, "sucessos": 0}  # estado durante a execução


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
        if volta and rota.get("volta_ate_max") and volta > rota["volta_ate_max"]:
            continue  # volta depois da data-limite
        if (volta and rota.get("estadia_max_dias")
                and (date.fromisoformat(volta) - date.fromisoformat(ida)).days > rota["estadia_max_dias"]):
            continue  # o Google não mostra voos para estadias longas demais
        combos.append({**rota, "origem": o, "destino": d, "ida": ida, "volta": volta})
    return combos


# --------------------------------------------------------------------------
# Regras
# --------------------------------------------------------------------------

def rota_expirada(rota, hoje=None):
    return not expandir(rota, hoje)


def decidir_aviso(preco, preco_alvo, ultimo_avisado, queda_minima=0):
    """Retorna (avisar, novo_ultimo_avisado).

    - Preço >= alvo: não avisa e limpa o histórico, para que uma nova queda
      abaixo do alvo gere aviso de novo.
    - Preço < alvo e nunca avisado: avisa.
    - Preço < alvo e pelo menos `queda_minima` abaixo do último avisado: avisa
      (caiu de novo). Quedas menores não avisam e não mudam a referência.
    - Caso contrário: não avisa e mantém o histórico.
    """
    if preco >= preco_alvo:
        return False, None
    if ultimo_avisado is None or preco <= ultimo_avisado - max(queda_minima, 1):
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


class SemVoos(RuntimeError):
    """O Google respondeu normalmente, mas sem nenhum voo: tentar de novo não adianta."""


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

    blocos = [b for b in (_pegar(payload, 2, 0), _pegar(payload, 3, 0)) if b]
    if not blocos:
        # Estrutura diferente da esperada: procura itens de voo em toda a página.
        blocos = [list(_procurar_itens(payload))]
    return _itens_para_voos(item for bloco in blocos for item in bloco or [])


def _itens_para_voos(itens):
    """Converte itens do JSON do Google ([voo, [[_, preço]], ...]) em Flights."""
    from fast_flights.model import Airport, Flights, SimpleDatetime, SingleFlight

    def hora(v):
        v = [*(v or []), None, None]
        return (v[0] or 0, v[1] or 0)

    resultados = []
    for item in itens:
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


# Para algumas rotas (ex.: saindo de aeroportos menores), o Google não manda os voos junto com
# a página: o navegador pede os resultados depois, numa chamada à parte
# (GetShoppingResults). Aqui abrimos a página num Chromium de verdade e lemos
# essa resposta.

def _json_aninhado(dados, profundidade=0):
    """Percorre listas e strings que contêm JSON, achando itens de voo."""
    if profundidade > 8:
        return
    if isinstance(dados, str):
        texto = dados.strip()
        if texto[:1] in "[{" and len(texto) > 50:
            try:
                yield from _json_aninhado(json.loads(texto), profundidade + 1)
            except ValueError:
                pass
        return
    if isinstance(dados, list):
        if _eh_item_voo(dados):
            yield dados
            return
        for x in dados:
            yield from _json_aninhado(x, profundidade + 1)


def voos_da_resposta_rpc(texto):
    """Voos de uma resposta GetShoppingResults (")]}'" + linhas de JSON)."""
    itens = []
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha.startswith("["):
            try:
                itens.extend(_json_aninhado(json.loads(linha)))
            except ValueError:
                continue
    vistos, unicos = set(), []
    for item in itens:  # a mesma opção pode vir repetida em blocos diferentes
        chave = json.dumps(item[0], sort_keys=True)[:500] + str(_pegar(item, 1, 0, 1))
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(item)
    return _itens_para_voos(unicos)


_pw = {}  # navegador aberto uma vez por execução e reaproveitado


def _navegador_aberto():
    """Contexto do Chromium (abre na primeira chamada; fecha ao sair do programa)."""
    if "contexto" not in _pw:
        import atexit

        from playwright.sync_api import sync_playwright

        _pw["playwright"] = sync_playwright().start()
        _pw["navegador"] = _pw["playwright"].chromium.launch(headless=True)
        _pw["contexto"] = _pw["navegador"].new_context(locale="pt-BR")
        atexit.register(_fechar_navegador)
    return _pw["contexto"]


def _fechar_navegador():
    try:
        _pw.pop("navegador").close()
        _pw.pop("playwright").stop()
    except Exception:
        pass
    _pw.clear()


def _buscar_com_navegador(params):
    """Abre a busca no Chromium e devolve os voos das respostas GetShoppingResults."""
    from urllib.parse import urlencode

    url = "https://www.google.com/travel/flights/search?" + urlencode(params)
    respostas = []
    pagina = _navegador_aberto().new_page()
    try:
        pagina.on("response", lambda r: respostas.append(r) if "GetShoppingResults" in r.url else None)
        pagina.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_SEGUNDOS * 1000)
        botao = pagina.get_by_role("button", name=re.compile(
            r"Aceitar tudo|Rejeitar tudo|Accept all|Reject all"))
        if botao.count():  # página de consentimento de cookies (só na primeira vez)
            botao.first.click(timeout=5000)
        voos, depois_da_resposta = [], 0
        for _ in range(30):  # espera os resultados chegarem (até ~15 s)
            pagina.wait_for_timeout(500)
            voos = _voos_das_respostas(respostas)
            if voos:
                pagina.wait_for_timeout(800)  # deixa chegar o resto da lista
                voos = _voos_das_respostas(respostas)
                break
            if respostas:
                depois_da_resposta += 1
                if depois_da_resposta >= 6:  # respondeu há 3 s e sem voos: desiste
                    break
        if not voos and not respostas:
            # nenhuma chamada à parte: às vezes os voos vêm na própria página
            try:
                voos = _extrair(pagina.content())
            except Exception:
                voos = []
        com_erro = not voos and any("ErrorResponse" in _texto(r) for r in respostas)
        if not voos and respostas and not com_erro:  # respondeu, mas não entendemos: guarda para análise
            PASTA_DEBUG.mkdir(exist_ok=True)
            n = len(list(PASTA_DEBUG.glob("navegador_*.txt")))
            if n < 5:
                (PASTA_DEBUG / f"navegador_{n}.txt").write_text(
                    url + "\n\n" + "\n---\n".join(_texto(r) for r in respostas), encoding="utf-8")
            if n == 0:
                print(f"  diagnóstico navegador: {_texto(respostas[0])[:400]!r}")
    finally:
        pagina.close()
    voos = [v for v in voos if v.price]
    if com_erro:
        # erro passageiro do Google (o mesmo "errorHasStatus" das consultas diretas)
        raise RuntimeError("o Google respondeu com erro (navegador)")
    if not voos and not respostas:
        raise RuntimeError("o Google não respondeu a tempo (navegador)")  # tenta de novo na 2ª rodada
    if not voos:
        raise SemVoos(f"o Google não mostra voos para estas datas (navegador: {len(respostas)} resposta(s))")
    return voos


def _texto(resposta):
    try:
        return resposta.text()
    except Exception as e:
        return f"(sem corpo: {e})"


def _navegador_ativo():
    # 4 falhas seguidas sem nenhum sucesso: desiste do navegador nesta execução
    return _navegador["sucessos"] > 0 or _navegador["falhas_seguidas"] < 4


def _navegador_com_controle(params):
    try:
        voos = _buscar_com_navegador(params)
    except Exception:
        _navegador["falhas_seguidas"] += 1
        raise
    _navegador["falhas_seguidas"] = 0
    _navegador["sucessos"] += 1
    return voos


def _voos_das_respostas(respostas):
    voos = []
    for r in list(respostas):
        try:
            voos.extend(voos_da_resposta_rpc(r.text()))
        except Exception:
            continue  # resposta ainda incompleta ou em outro formato
    return voos


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
        raise SemVoos("o Google não mostra voos para estas datas")
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

    # Rota configurada com "navegador_direto", ou origem que já precisou do
    # navegador nesta execução: vai direto para ele.
    if combo and USAR_NAVEGADOR and (combo.get("navegador_direto") or combo["origem"] in _navegador["direto"]):
        try:
            return min(_navegador_com_controle(google), key=lambda r: r.price)
        except Exception:
            if combo.get("navegador_direto") and _navegador_ativo():
                raise  # as consultas diretas não funcionam nesta rota: nem tenta
            # senão, cai nas tentativas normais

    ultimo_erro, html = None, None
    for i, (nome, params) in enumerate(tentativas):
        if i == 2 and isinstance(ultimo_erro, SemVoos):
            break  # as duas consultas responderam sem voos: não é falha passageira
        if i and PAUSA_SEGUNDOS:
            time.sleep(PAUSA_SEGUNDOS * (4 if i == 2 else 1))
        try:
            html = _baixar(params)
            resultados = _extrair(html)
        except Exception as e:
            ultimo_erro = e
            continue
        return min(resultados, key=lambda r: r.price)
    if combo and isinstance(ultimo_erro, SemVoos) and USAR_NAVEGADOR and _navegador_ativo():
        try:
            voos = _navegador_com_controle(google)
            _navegador["direto"].add(combo["origem"])
            return min(voos, key=lambda r: r.price)
        except Exception as e:
            ultimo_erro = e if isinstance(e, RuntimeError) else RuntimeError(f"navegador: {e}")
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


def fmt_preco(valor, moeda):
    """5320, "BRL" -> "R$ 5.320"; 846, "EUR" -> "846 €"."""
    n = f"{int(valor):,}".replace(",", ".")
    return {"BRL": f"R$ {n}", "EUR": f"{n} €", "USD": f"US$ {n}"}.get(moeda, f"{n} {moeda}")


def _fmt_data(iso):
    a, m, d = iso.split("-")
    return f"{d}/{m}/{a}"


def montar_email(rota, combo, moeda, preco, resumo, link, ultimo_avisado, stats=None,
                 opcoes=None, modo_resumo=False, cobertura=None):
    """Devolve (assunto, texto, html). opcoes = [(combo, voo, link), ...] mais baratas."""
    P = lambda v: fmt_preco(v, moeda)  # noqa: E731
    trajeto = f"{combo['origem']} → {combo['destino']}"
    excelente = rota.get("preco_excelente")
    titulo = f"{rota['nome']}: " if rota.get("nome") else ""
    if modo_resumo:
        selo = "📊 Resumo"
    else:
        selo = "🔥 PREÇO EXCELENTE" if excelente and preco <= excelente else "✈️"
    assunto = f"{selo} {titulo}{trajeto} {P(preco)} (alvo {P(rota['preco_alvo'])})"

    datas = f"Ida {_fmt_data(combo['ida'])}" + (
        f" · Volta {_fmt_data(combo['volta'])}" if combo.get("volta") else " (só ida)")
    escalas = "direto" if resumo["escalas"] == 0 else (
        f"{resumo['escalas']} ({', '.join(resumo['conexoes'])})"
    )
    faixa = f"abaixo de {P(rota['preco_alvo'])}"
    if excelente:
        faixa += f" (excelente: até {P(excelente)})"
    if modo_resumo:
        chamada = (f"Resumo da busca: o melhor preço agora é {P(preco)}"
                   + (" (abaixo do alvo)." if preco < rota["preco_alvo"] else " (ainda não está abaixo do alvo)."))
    else:
        chamada = f"Passagem abaixo do preço-alvo: {trajeto} por {P(preco)}"

    detalhes = [
        ("Preço", f"{P(preco)} ({rota.get('adultos', 1)} adulto(s), ida e volta)" if combo.get("volta") else f"{P(preco)} ({rota.get('adultos', 1)} adulto(s))"),
        ("Preço-alvo", faixa),
    ]
    if ultimo_avisado is not None and not modo_resumo:
        detalhes.append(("Último aviso", f"{P(ultimo_avisado)} (o preço caiu de novo)"))
    if stats:
        detalhes.append(("Histórico", f"mínimo {P(stats['min'])} / mediana {P(stats['mediana'])} "
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
    linhas = ([f"== {rota['nome']} =="] if rota.get("nome") else []) + [
        chamada, "", f"Abrir esta busca no Google Flights: {link}", ""]
    linhas += [f"{k + ':':<15}{v}" for k, v in detalhes]
    if len(opcoes) > 1:
        linhas += ["", "Outras datas mais baratas:"]
        for c, v, l in opcoes:
            volta = f" → {_fmt_data(c['volta'])}" if c.get("volta") else ""
            linhas.append(f"  {_fmt_data(c['ida'])}{volta}: {P(v.price)}  {l}")
    texto = "\n".join(linhas)

    if cobertura and cobertura[0] < cobertura[1]:
        texto += f"\n\nPreços encontrados para {cobertura[0]} de {cobertura[1]} combinações de datas."
    html = _cartao_html(rota, combo, moeda, preco, resumo, link, ultimo_avisado, stats,
                        opcoes, modo_resumo, cobertura)
    return assunto, texto, html


# --------------------------------------------------------------------------
# HTML do e-mail (tabelas + estilos inline: é o que Gmail/Outlook respeitam)
# --------------------------------------------------------------------------

_DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
_MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
_FONTE = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif"


def _data_curta(iso):
    d = date.fromisoformat(iso)
    return f"{_DIAS[d.weekday()]}, {d.day} {_MESES[d.month - 1]}"


def _horario(resumo):
    """'07:55 → 06:10 (+1)' a partir de partida/chegada 'dd/mm/aaaa hh:mm'."""
    try:
        dp, hp = resumo["partida"].split()
        dc, hc = resumo["chegada"].split()
    except ValueError:
        return ""
    mais = ""
    if dp != dc:
        dias = (date(*map(int, reversed(dc.split("/")))) - date(*map(int, reversed(dp.split("/"))))).days
        mais = f" (+{dias})" if dias > 0 else ""
    return f"{hp} → {hc}{mais}"


def _cartao_html(rota, combo, moeda, preco, resumo, link, ultimo_avisado, stats, opcoes,
                 modo_resumo, cobertura=None):
    from html import escape as e

    P = lambda v: e(fmt_preco(v, moeda))  # noqa: E731
    alvo, excelente = rota["preco_alvo"], rota.get("preco_excelente")
    if excelente and preco <= excelente:
        selo, fundo, cor = "🔥 Preço excelente", "#fce8e6", "#c5221f"
    elif preco < alvo:
        selo, fundo, cor = "✓ Abaixo do alvo", "#e6f4ea", "#137333"
    else:
        selo, fundo, cor = f"Acima do alvo · faltam {fmt_preco(preco - alvo + 1, moeda)}", "#fef7e0", "#b06000"

    volta = combo.get("volta")
    tipo = "ida e volta" if volta else "só ida"
    adultos = rota.get("adultos", 1)
    pessoas = f"{adultos} adulto{'s' if adultos > 1 else ''}"
    escalas = ("voo direto" if resumo["escalas"] == 0 else
               f"{resumo['escalas']} escala{'s' if resumo['escalas'] > 1 else ''} "
               f"({', '.join(resumo['conexoes'])})")
    horario = _horario(resumo)

    rotulo = 'font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#5f6368'
    col_volta = (
        f'<td width="50%" style="padding:14px 16px;vertical-align:top">'
        f'<div style="{rotulo}">Volta</div>'
        f'<div style="font-size:16px;font-weight:600;margin-top:4px">{e(_data_curta(volta))}</div>'
        f'<div style="font-size:13px;color:#5f6368;margin-top:2px">'
        f'{e(combo["destino"])} → {e(combo["origem"])}</div></td>'
    ) if volta else ""
    trechos = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f8f9fa;border-radius:12px;margin-top:20px"><tr>'
        f'<td width="50%" style="padding:14px 16px;vertical-align:top;'
        f'{"border-right:1px solid #e8eaed" if volta else ""}">'
        f'<div style="{rotulo}">Ida</div>'
        f'<div style="font-size:16px;font-weight:600;margin-top:4px">{e(_data_curta(combo["ida"]))}</div>'
        f'<div style="font-size:13px;color:#5f6368;margin-top:2px">{e(horario) or "&nbsp;"}</div></td>'
        f'{col_volta}</tr>'
        f'<tr><td colspan="2" style="padding:12px 16px;border-top:1px solid #e8eaed;'
        f'font-size:13px;color:#3c4043">✈ {e(resumo["companhia"])} &nbsp;·&nbsp; {e(escalas)}</td></tr>'
        f'</table>'
    )

    botao = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:20px">'
             f'<tr><td style="background:#1a73e8;border-radius:24px">'
             f'<a href="{e(link)}" style="display:inline-block;padding:13px 26px;color:#ffffff;'
             f'text-decoration:none;font-weight:600;font-size:15px">Ver voos no Google Flights →</a>'
             f'</td></tr></table>')

    lista = ""
    if len(opcoes) > 1:
        linhas = []
        for n, (c, v, l) in enumerate(opcoes):
            destaque = "background:#e8f0fe;" if n == 0 else ""
            datas = _data_curta(c["ida"]) + (f" → {_data_curta(c['volta'])}" if c.get("volta") else "")
            linhas.append(
                f'<tr><td style="{destaque}padding:10px 12px;border-radius:8px 0 0 8px;font-size:14px">'
                f'{e(datas)}</td>'
                f'<td align="right" style="{destaque}padding:10px 8px;font-size:14px;font-weight:600;'
                f'white-space:nowrap">{P(v.price)}</td>'
                f'<td align="right" style="{destaque}padding:10px 12px;border-radius:0 8px 8px 0;'
                f'white-space:nowrap"><a href="{e(l)}" style="color:#1a73e8;text-decoration:none;'
                f'font-size:13px;font-weight:600">ver ›</a></td></tr>')
        lista = (f'<div style="{rotulo};margin-top:28px;margin-bottom:6px">Datas mais baratas</div>'
                 f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                 f'{"".join(linhas)}</table>')

    rodape = [f"Alvo: abaixo de {P(alvo)}" + (f" · excelente até {P(excelente)}" if excelente else "")]
    if ultimo_avisado is not None and not modo_resumo:
        rodape.append(f"Último aviso: {P(ultimo_avisado)} — o preço caiu de novo")
    if cobertura and cobertura[0] < cobertura[1]:
        rodape.append(f"Preços encontrados para {cobertura[0]} de {cobertura[1]} combinações de datas "
                      "(as outras ficaram sem resposta do Google ou sem tempo nesta busca)")
    if stats and stats["n"] > 1:
        rodape.append(f"Últimos {DIAS_CONSULTAS} dias: mínimo {P(stats['min'])} · "
                      f"mediana {P(stats['mediana'])} ({stats['n']} consultas)")

    nome = (f'<div style="{rotulo}">{e(rota["nome"])}</div>') if rota.get("nome") else ""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#ffffff;border-radius:16px;box-shadow:0 1px 3px rgba(60,64,67,.15)">'
        f'<tr><td style="padding:28px 28px 24px;{_FONTE};color:#202124">'
        f'{nome}'
        f'<div style="font-size:22px;font-weight:700;margin-top:6px">'
        f'{e(combo["origem"])} <span style="color:#1a73e8">✈</span> {e(combo["destino"])}</div>'
        f'<div style="font-size:40px;font-weight:800;letter-spacing:-.02em;margin-top:18px;'
        f'line-height:1">{P(preco)}</div>'
        f'<div style="font-size:13px;color:#5f6368;margin-top:6px">{tipo} · {pessoas} · menor preço encontrado</div>'
        f'<div style="margin-top:14px"><span style="display:inline-block;background:{fundo};'
        f'color:{cor};font-size:13px;font-weight:600;padding:6px 12px;border-radius:999px">'
        f'{e(selo)}</span></div>'
        f'{trechos}{botao}{lista}'
        f'<div style="margin-top:24px;padding-top:16px;border-top:1px solid #e8eaed;'
        f'font-size:12px;line-height:1.6;color:#5f6368">{"<br>".join(rodape)}</div>'
        f'</td></tr></table>'
    )


def enviar_email(assunto, corpo, html=None):
    remetente = os.environ["EMAIL_REMETENTE"].strip()
    # A senha de app do Gmail aparece com espaços ("abcd efgh ijkl mnop") e o
    # Secret pode ter sido colado com quebra de linha: remove tudo isso.
    senha = "".join(os.environ["EMAIL_SENHA"].split())
    # EMAIL_DESTINO aceita vários endereços separados por vírgula.
    destinos = [d.strip() for d in os.environ["EMAIL_DESTINO"].replace(";", ",").split(",") if d.strip()]
    # Diagnóstico sem expor as credenciais.
    print(f"  e-mail: remetente @{remetente.rpartition('@')[2] or '(sem @!)'}, "
          f"senha com {len(senha)} caracteres"
          f"{'' if senha.isascii() and senha.isalpha() else ' (não são só letras: senha de app tem só letras)'}, "
          f"{len(destinos)} destinatário(s): "
          + ", ".join(f"@{d.rpartition('@')[2] or '(sem @!)'}" for d in destinos))

    if len(senha) != 16:
        raise RuntimeError(
            f"EMAIL_SENHA tem {len(senha)} caracteres, mas a senha de app do Gmail tem 16 letras: "
            "gere uma nova em https://myaccount.google.com/apppasswords e atualize o Secret"
        )

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = ", ".join(destinos)
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


def buscar_combinacoes(chave, combos, moeda, buscar, montar, limite=None):
    """Busca cada combinação e devolve (combo, query, voo, todos) da mais barata.

    Falhas individuais são registradas e ignoradas; só lança exceção se
    todas as combinações falharem. `limite` (time.monotonic) é o horário em
    que esta rota para de buscar e fica com o que já achou.
    """
    melhor, pendentes, erro, todos = None, list(combos), None, []
    sem_tempo = []
    # O Google falha de forma passageira em algumas datas (e mais quando recebe
    # muitas buscas seguidas): as que falharem são tentadas de novo em mais duas
    # rodadas, com pausas cada vez maiores.
    for rodada in (1, 2, 3):
        falharam = []
        if rodada > 1 and pendentes and PAUSA_SEGUNDOS:
            time.sleep(PAUSA_SEGUNDOS * (5 if rodada == 2 else 15))
        for i, combo in enumerate(pendentes):
            if limite is not None and time.monotonic() >= limite:
                sem_tempo = pendentes[i:] if rodada == 1 else []
                if rodada > 1:
                    falharam.extend(pendentes[i:])
                break
            if i and PAUSA_SEGUNDOS:
                time.sleep(PAUSA_SEGUNDOS)  # evita bombardear o Google
            try:
                query = montar(combo, moeda)
                voo = buscar(query)
            except SemVoos as e:
                erro = e  # resposta definitiva: não entra na segunda rodada
                print(f"[{chave}]   {descrever(combo)}: {e}")
                continue
            except Exception as e:
                falharam.append(combo)
                erro = e
                continue
            print(f"[{chave}]   {descrever(combo)}: {fmt_preco(voo.price, moeda)}")
            todos.append((combo, voo))
            if melhor is None or voo.price < melhor[2].price:
                melhor = (combo, query, voo)
        pendentes = falharam
        if not pendentes or sem_tempo or (limite is not None and time.monotonic() >= limite):
            break
    for combo in pendentes:
        print(f"[{chave}]   {descrever(combo)}: sem resposta do Google ({erro})")
    if sem_tempo:
        print(f"[{chave}]   tempo da rota esgotado: {len(sem_tempo)} combinação(ões) não consultada(s)")
    if melhor is None:
        raise erro or RuntimeError("tempo esgotado antes de encontrar preços")
    todos.sort(key=lambda cv: cv[1].price)
    return (*melhor, todos)


def processar_rota(rota, moeda, historico, buscar=None, montar=None,
                   consultas=None, hoje=None, modo_resumo=False, limite=None):
    """Busca uma rota e devolve a seção do e-mail (ou None se não há o que avisar).

    A seção traz "aviso": o que gravar no histórico depois que o e-mail sair.
    Lança exceção se nenhuma busca da rota funcionar.
    """
    buscar = buscar or buscar_mais_barato
    montar = montar or montar_query
    consultas = consultas if consultas is not None else {}

    chave = chave_rota(rota)
    combos = expandir(rota, hoje)
    combo, query, voo, todos = buscar_combinacoes(chave, combos, moeda, buscar, montar, limite)
    preco = int(voo.price)

    registros = consultas.setdefault(chave, [])
    registros.append({
        "em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preco": preco,
        "moeda": moeda,
        "origem": combo["origem"],
        "destino": combo["destino"],
        "ida": combo["ida"],
        "volta": combo.get("volta"),
    })

    aviso = historico.get(chave, {})
    anterior = aviso.get("ultimo_preco_avisado") if aviso.get("moeda", moeda) == moeda else None
    avisar, novo = decidir_aviso(preco, rota["preco_alvo"], anterior, rota.get("queda_minima", 0))
    print(f"[{chave}] menor preço: {fmt_preco(preco, moeda)} em {descrever(combo)} "
          f"(alvo {fmt_preco(rota['preco_alvo'], moeda)}, último aviso "
          f"{fmt_preco(anterior, moeda) if anterior else 'nenhum'})")

    if novo is None:
        historico.pop(chave, None)  # acima do alvo: a próxima queda avisa de novo
    if not avisar and not modo_resumo:
        return None

    # consultas antigas, sem o campo "moeda", foram feitas em EUR
    mesmos = [r for r in registros if r.get("moeda", "EUR") == moeda]
    opcoes = [(c, v, link_busca(c, moeda)) for c, v in todos[:5]]
    assunto, texto, html = montar_email(
        rota, combo, moeda, preco, resumir_voo(voo), link_busca(combo, moeda),
        anterior, estatisticas(mesmos), opcoes, modo_resumo, (len(todos), len(combos)))
    return {
        "chave": chave, "assunto": assunto, "texto": texto, "html": html,
        "aviso": {"ultimo_preco_avisado": novo, "moeda": moeda} if avisar and not modo_resumo else None,
    }


def juntar_secoes(secoes, titulo="Alerta de passagens"):
    """Um e-mail só com as seções de todas as rotas."""
    assunto = secoes[0]["assunto"]
    if len(secoes) > 1:
        assunto += f" (+{len(secoes) - 1} rota{'s' if len(secoes) > 2 else ''})"
    texto = ("\n\n" + "-" * 40 + "\n\n").join(s["texto"] for s in secoes)
    cartoes = '<div style="height:20px;line-height:20px">&nbsp;</div>'.join(s["html"] for s in secoes)
    agora = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y às %H:%M")
    html = (
        f'<div style="margin:0;padding:0;background:#f1f3f4">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f1f3f4"><tr><td align="center" style="padding:28px 12px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px"><tr><td style="padding:0 4px 16px;{_FONTE};font-size:14px;'
        f'color:#5f6368"><b style="color:#202124">✈️ {titulo}</b> · busca de {agora}</td></tr>'
        f'<tr><td>{cartoes}</td></tr>'
        f'<tr><td style="padding:20px 4px 0;{_FONTE};font-size:11px;line-height:1.6;color:#80868b">'
        f'Preços do Google Flights no momento da busca; podem mudar. Datas e alvos ficam em '
        f'rotas.json no repositório.</td></tr>'
        f'</table></td></tr></table></div>'
    )
    return assunto, texto, html


def main(buscar=None, enviar=None, montar=None, hoje=None, modo_resumo=False):
    enviar = enviar or enviar_email
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

    falhas, secoes = 0, []
    restantes = [len(expandir(r, hoje)) for r in ativas]
    for n, rota in enumerate(ativas):
        rota = {"queda_minima": config.get("queda_minima", 0), **rota}
        limite = None
        if PRAZO_MINUTOS:  # cada rota fica com uma fatia do tempo que sobra
            fatia = segundos_restantes() * restantes[n] / max(sum(restantes[n:]), 1)
            limite = time.monotonic() + max(fatia, 0)
        try:
            secao = processar_rota(rota, moeda, historico, buscar, montar, consultas, hoje,
                                   modo_resumo, limite)
        except Exception as e:  # uma rota com erro não derruba as outras
            falhas += 1
            print(f"[{chave_rota(rota)}] ERRO: {e}", file=sys.stderr)
            if not isinstance(e, RuntimeError):  # erro inesperado: mostra detalhes
                traceback.print_exc()
            continue
        if secao:
            secoes.append(secao)

    erro_email = False
    if secoes:
        try:
            enviar(*juntar_secoes(secoes, config.get("titulo", "Alerta de passagens")))
            print(f"E-mail enviado ({len(secoes)} rota(s)).")
            agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for secao in secoes:  # só grava o aviso depois que o e-mail saiu
                if secao["aviso"]:
                    historico[secao["chave"]] = {**secao["aviso"], "avisado_em": agora}
        except Exception as e:
            erro_email = True
            print(f"ERRO no e-mail: {e}", file=sys.stderr)

    salvar()

    if falhas == len(ativas):
        print("Todas as rotas falharam.", file=sys.stderr)
        return 1
    return 1 if erro_email else 0


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
