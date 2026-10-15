# Alerta de passagens aéreas

Consulta o Google Flights (via [fast-flights](https://github.com/AWeirdDev/fast-flights) 3.1.0, sem chave de API) a cada 6 horas pelo GitHub Actions e manda um e-mail quando o preço fica abaixo do alvo.

## Como funciona

- `rotas.json`: moeda e lista de rotas (`origem`, `destino`, `ida`, `volta` opcional, `adultos`, `max_escalas` (`null` = qualquer), `preco_alvo`, e opcionalmente `nome` e `preco_excelente`).
  - `origem`, `destino`, `ida` e `volta` aceitam um valor ou uma lista. Todas as combinações são consultadas (com uma pausa de 3 s entre elas) e vale a mais barata, com um único e-mail por rota.
  - Para datas flexíveis, use `ida_de` e `ida_ate` (todas as datas do intervalo) no lugar de `ida`, e `estadia_dias` (número ou lista) no lugar de `volta`: a volta será a ida mais a estadia.
  - Abaixo de `preco_alvo`, chega o aviso. Até `preco_excelente`, o assunto do e-mail vem marcado com 🔥.
- `alerta.py`: busca o voo mais barato de cada rota e manda e-mail se o preço estiver abaixo do `preco_alvo`. Depois de um aviso, só avisa de novo se o preço cair ainda mais. Se o preço voltar a ficar acima do alvo, o histórico da rota é limpo.
- `historico.json`: último preço avisado por rota.
- `consultas.json`: menor preço encontrado em cada execução (com a combinação), guardado por 90 dias. O e-mail mostra o mínimo e a mediana desse período.
- O workflow faz commit de `historico.json` e `consultas.json` de volta no repositório.
- Rotas com data de ida já passada são ignoradas. Um erro numa rota não afeta as outras. Se todas falharem, o job termina com erro.

## Configuração

1. Ative a verificação em duas etapas no Gmail e crie uma **senha de app** (https://myaccount.google.com/apppasswords).
2. No repositório, vá em *Settings → Secrets and variables → Actions* e crie:
   - `EMAIL_REMETENTE`: Gmail que envia
   - `EMAIL_SENHA`: senha de app
   - `EMAIL_DESTINO`: quem recebe
3. Em *Settings → Actions → General → Workflow permissions*, deixe marcado **Read and write permissions**.
4. Para rodar manualmente: aba *Actions → Alerta de passagens → Run workflow*. Marque **"Só enviar um e-mail de teste"** para conferir os Secrets sem buscar preços.

## Rodar localmente

```bash
pip install -r requirements.txt
export EMAIL_REMETENTE=... EMAIL_SENHA=... EMAIL_DESTINO=...
python alerta.py
```
