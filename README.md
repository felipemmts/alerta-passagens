# Alerta de passagens aéreas

Consulta o Google Flights (via [fast-flights](https://github.com/AWeirdDev/fast-flights) 3.1.0, sem chave de API) a cada 6 horas pelo GitHub Actions e manda um e-mail quando o preço fica abaixo do alvo.

## Como funciona

- `rotas.json`: moeda e lista de rotas (`origem`, `destino`, `ida`, `volta` opcional, `adultos`, `max_escalas` (`null` = qualquer), `preco_alvo`, e opcionalmente `nome` e `preco_excelente`).
  - `origem`, `destino`, `ida` e `volta` aceitam um valor ou uma lista. Todas as combinações são consultadas (com uma pausa de 3 s entre elas) e vale a mais barata, com um único e-mail por rota.
  - Para datas flexíveis, use `ida_de` e `ida_ate` (todas as datas do intervalo) no lugar de `ida`, e `volta_de`/`volta_ate` (todas as combinações de ida e volta) ou `estadia_dias` (número ou lista: a volta é a ida mais a estadia) no lugar de `volta`.
  - Abaixo de `preco_alvo`, chega o aviso. Até `preco_excelente`, o assunto do e-mail vem marcado com 🔥.
- `alerta.py`: busca o voo mais barato de cada rota e manda e-mail se o preço estiver abaixo do `preco_alvo`. Depois de um aviso, só avisa de novo se o preço cair pelo menos `queda_minima` (em rotas.json; hoje R$ 50) abaixo do último preço avisado. Se o preço voltar a ficar acima do alvo, o histórico da rota é limpo.
- Quando mais de uma rota tem novidade, sai um e-mail só, com uma parte para cada rota.
- `EMAIL_DESTINO` aceita vários endereços separados por vírgula (ex.: `eu@gmail.com, outra@gmail.com`).
- `historico.json`: último preço avisado por rota.
- `consultas.json`: menor preço encontrado em cada execução (com a combinação), guardado por 90 dias. O e-mail mostra o mínimo e a mediana desse período.
- O workflow faz commit de `historico.json` e `consultas.json` de volta no repositório.
- Rotas com data de ida já passada são ignoradas. Um erro numa rota não afeta as outras. Se todas falharem, o job termina com erro.

- Cada busca tem até 3 tentativas, um limite de 40 s, e as datas que falharem são tentadas de novo numa segunda rodada. Se ainda assim falharem, ficam de fora e a execução segue. O envio de e-mail também tem 3 tentativas.
- O e-mail traz um botão que abre a busca direto no Google Flights e as 5 datas mais baratas, cada uma com seu link.

## Alerta FLN → Espanha

Um segundo workflow (`.github/workflows/alerta_fln.yml`) faz a mesma coisa que o de São Paulo (GRU → Madri e Barcelona), mas saindo de Florianópolis, com arquivos próprios: `rotas_fln.json`, `historico_fln.json`, `consultas_fln.json` e `ultima_execucao_fln.log`. Ele roda meia hora depois do alerta de São Paulo, usa os mesmos Secrets e é independente: um não afeta o outro. `estadia_max_dias` pula combinações com estadia longa demais, para as quais o Google não mostra voos.

## Configuração

1. Ative a verificação em duas etapas no Gmail e crie uma **senha de app** (https://myaccount.google.com/apppasswords).
2. No repositório, vá em *Settings → Secrets and variables → Actions* e crie:
   - `EMAIL_REMETENTE`: Gmail que envia
   - `EMAIL_SENHA`: senha de app
   - `EMAIL_DESTINO`: quem recebe
3. Em *Settings → Actions → General → Workflow permissions*, deixe marcado **Read and write permissions**.
4. Para rodar manualmente: aba *Actions → Alerta de passagens → Run workflow*. Marque **"Só enviar um e-mail de teste"** para conferir os Secrets sem buscar preços, ou **"Buscar e mandar e-mail com os preços atuais"** para receber um resumo mesmo com o preço acima do alvo.

## Rodar localmente

```bash
pip install -r requirements.txt
export EMAIL_REMETENTE=... EMAIL_SENHA=... EMAIL_DESTINO=...
python alerta.py
```
