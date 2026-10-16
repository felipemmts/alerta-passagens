# Alerta de passagens aéreas

Consulta o Google Flights pelo GitHub Actions (via [fast-flights](https://github.com/AWeirdDev/fast-flights) 3.1.0, sem chave de API) e manda um e-mail quando o preço de uma rota fica abaixo do alvo.

## Como funciona

- `rotas.json`: título do e-mail, moeda, `queda_minima` e a lista de buscas. Cada busca tem `origem`, `destino`, datas, `adultos`, `max_escalas` (`null` = qualquer), `preco_alvo` e, opcionalmente, `nome` e `preco_excelente`.
  - `origem` e `destino` aceitam um código de aeroporto ou uma lista.
  - Datas de ida: `ida` (uma data ou lista) ou o intervalo `ida_de`/`ida_ate`.
  - Volta: `volta` (data ou lista), o intervalo `volta_de`/`volta_ate`, ou `estadia_dias` (número ou lista: a volta é a ida mais a estadia). `volta_ate_max` descarta voltas depois dessa data.
  - Todas as combinações são consultadas e vale a mais barata.
- Avisa quando o preço fica abaixo de `preco_alvo` (🔥 até `preco_excelente`). Depois de um aviso, só avisa de novo se cair pelo menos `queda_minima` abaixo do último preço avisado. Se o preço voltar a ficar acima do alvo, a próxima queda avisa de novo.
- O e-mail traz um botão que abre a busca no Google Flights e as 5 combinações mais baratas, cada uma com seu link. Quando há mais de uma busca com novidade, sai um e-mail só.
- Quando a consulta direta não traz voos (acontece em algumas rotas e em estadias longas), a busca é feita num navegador (Playwright). `navegador_direto: true` pula a consulta direta.
- Busca em lotes (`LOTE` no workflow): cada execução consulta só parte das combinações, salteadas pelo período todo, e a próxima continua de onde parou (`progresso.json`). O e-mail considera os preços das últimas 36 h de todos os lotes.
- Se o Google falhar 3 vezes seguidas, a execução para na hora e o que faltou volta para a fila. Há também um prazo (`PRAZO_MINUTOS`): perto dele, para de buscar e envia o que já tem.
- `historico.json`: último preço avisado por busca.
- `progresso.json`: fila de combinações ainda não consultadas no ciclo e últimos preços de cada uma.
- `consultas.json`: menor preço de cada execução, guardado por 90 dias. O e-mail mostra o mínimo e a mediana do período.
- `ultima_execucao.log`: log da última execução.
- O workflow faz commit desses arquivos de volta no repositório.

Exemplo de busca: ida de Lisboa para Vancouver em qualquer dia de 10 a 15/03, ficando 20 ou 25 dias:

```json
{
  "titulo": "Alerta de passagens",
  "moeda": "EUR",
  "queda_minima": 30,
  "rotas": [
    {
      "nome": "Exemplo",
      "origem": "LIS",
      "destino": "YVR",
      "ida_de": "2027-03-10",
      "ida_ate": "2027-03-15",
      "estadia_dias": [20, 25],
      "adultos": 1,
      "max_escalas": 1,
      "preco_alvo": 900,
      "preco_excelente": 750
    }
  ]
}
```

## Configuração

1. Ative a verificação em duas etapas no Gmail e crie uma **senha de app** (https://myaccount.google.com/apppasswords).
2. Em *Settings → Secrets and variables → Actions*, crie:
   - `EMAIL_REMETENTE`: Gmail que envia
   - `EMAIL_SENHA`: senha de app (16 letras)
   - `EMAIL_DESTINO`: quem recebe (vários endereços separados por vírgula)
3. Em *Settings → Actions → General → Workflow permissions*, marque **Read and write permissions**.
4. Para rodar manualmente: *Actions → Alerta de passagens → Run workflow*. Opções: **"Só enviar um e-mail de teste"** (confere os Secrets) e **"Buscar e mandar e-mail com os preços atuais"** (resumo, mesmo acima do alvo).

## Rodar localmente

```bash
pip install -r requirements.txt
python -m playwright install chromium
export EMAIL_REMETENTE=... EMAIL_SENHA=... EMAIL_DESTINO=...
USAR_NAVEGADOR=true python alerta.py
```
