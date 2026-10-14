"""Alerta de preço de passagens aéreas (Google Flights via fast-flights 3.x).

Lê as rotas de rotas.json, busca o voo mais barato de cada uma e envia um
e-mail pelo Gmail quando o preço fica abaixo do preco_alvo (ou cai de novo
depois de um aviso). O último preço avisado fica em historico.json.
"""

import json
import os
import smtplib
import ssl
import sys
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARQ_ROTAS = BASE / "rotas.json"
ARQ_HISTORICO = BASE / "historico.json"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORTA = 465


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


def chave_rota(rota):
    volta = rota.get("volta") or "so-ida"
    return f"{rota['origem']}-{rota['destino']}-{rota['ida']}-{volta}-{rota.get('adultos', 1)}ad"


# --------------------------------------------------------------------------
# Regras
# --------------------------------------------------------------------------

def rota_expirada(rota, hoje=None):
    hoje = hoje or date.today()
    return date.fromisoformat(rota["ida"]) < hoje


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


def buscar_mais_barato(query):
    """Retorna o resultado mais barato (objeto com .price/.airlines/.flights)."""
    from fast_flights import get_flights

    resultados = [r for r in get_flights(query) if getattr(r, "price", 0)]
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


def montar_email(rota, moeda, preco, resumo, link, ultimo_avisado):
    trajeto = f"{rota['origem']} → {rota['destino']}"
    assunto = f"✈️ {trajeto}: {preco} {moeda} (alvo {rota['preco_alvo']} {moeda})"

    datas = f"Ida {rota['ida']}" + (f" · Volta {rota['volta']}" if rota.get("volta") else " (só ida)")
    escalas = "direto" if resumo["escalas"] == 0 else (
        f"{resumo['escalas']} ({', '.join(resumo['conexoes'])})"
    )
    linhas = [
        f"Passagem abaixo do preço-alvo: {trajeto}",
        "",
        f"Preço:      {preco} {moeda} ({rota.get('adultos', 1)} adulto(s))",
        f"Preço-alvo: {rota['preco_alvo']} {moeda}",
    ]
    if ultimo_avisado is not None:
        linhas.append(f"Último aviso: {ultimo_avisado} {moeda} (o preço caiu de novo)")
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

def processar_rota(rota, moeda, historico, buscar=None, enviar=None, montar=None):
    """Processa uma rota. Retorna True se avisou. Lança exceção em falha."""
    buscar = buscar or buscar_mais_barato
    enviar = enviar or enviar_email
    montar = montar or montar_query

    chave = chave_rota(rota)
    query = montar(rota, moeda)
    voo = buscar(query)
    preco = int(voo.price)

    anterior = historico.get(chave, {}).get("ultimo_preco_avisado")
    avisar, novo = decidir_aviso(preco, rota["preco_alvo"], anterior)
    print(f"[{chave}] menor preço: {preco} {moeda} (alvo {rota['preco_alvo']}, último aviso {anterior})")

    if avisar:
        assunto, corpo = montar_email(rota, moeda, preco, resumir_voo(voo), query.url(), anterior)
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

    ativas = [r for r in rotas if not rota_expirada(r, hoje)]
    for r in rotas:
        if r not in ativas:
            print(f"[{chave_rota(r)}] ignorada: data de ida já passou.")
            historico.pop(chave_rota(r), None)

    if not ativas:
        print("Nenhuma rota ativa.")
        salvar_json(ARQ_HISTORICO, historico)
        return 0

    falhas = 0
    for rota in ativas:
        try:
            processar_rota(rota, moeda, historico, buscar, enviar, montar)
        except Exception as e:  # uma rota com erro não derruba as outras
            falhas += 1
            print(f"[{chave_rota(rota)}] ERRO: {type(e).__name__}: {e}", file=sys.stderr)

    salvar_json(ARQ_HISTORICO, historico)

    if falhas == len(ativas):
        print("Todas as rotas falharam.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
