# Alerta de passagens aéreas

Consulta o Google Flights (via [fast-flights](https://github.com/AWeirdDev/fast-flights) 3.1.0, sem chave de API) a cada 6 horas pelo GitHub Actions e manda um e-mail quando o preço fica abaixo do alvo.

## Como funciona

- `rotas.json`: moeda e lista de rotas (`origem`, `destino`, `ida`, `volta` opcional, `adultos`, `max_escalas` (`null` = qualquer), `preco_alvo`).
- `alerta.py`: busca o voo mais barato de cada rota e manda e-mail se o preço estiver abaixo do `preco_alvo`. Depois de um aviso, só avisa de novo se o preço cair ainda mais. Se o preço voltar a ficar acima do alvo, o histórico da rota é limpo.
- `historico.json`: último preço avisado por rota. O workflow faz commit desse arquivo de volta no repositório.
- Rotas com data de ida já passada são ignoradas. Um erro numa rota não afeta as outras. Se todas falharem, o job termina com erro.

## Configuração

1. Ative a verificação em duas etapas no Gmail e crie uma **senha de app** (https://myaccount.google.com/apppasswords).
2. No repositório, vá em *Settings → Secrets and variables → Actions* e crie:
   - `EMAIL_REMETENTE`: Gmail que envia
   - `EMAIL_SENHA`: senha de app
   - `EMAIL_DESTINO`: quem recebe
3. Em *Settings → Actions → General → Workflow permissions*, deixe marcado **Read and write permissions**.
4. Para rodar manualmente: aba *Actions → Alerta de passagens → Run workflow*.

## Rodar localmente

```bash
pip install -r requirements.txt
export EMAIL_REMETENTE=... EMAIL_SENHA=... EMAIL_DESTINO=...
python alerta.py
```
