#!/usr/bin/env bash
# Faz commit dos arquivos de estado (histórico, consultas, fila e log) de volta no repositório.
set -u
if git diff --quiet -- historico.json consultas.json progresso.json ultima_execucao.log \
   && git ls-files --error-unmatch ultima_execucao.log >/dev/null 2>&1; then
  echo "Histórico sem alterações."
  exit 0
fi
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
touch progresso.json
git add historico.json consultas.json progresso.json ultima_execucao.log
git commit -q -m "Atualiza histórico e consultas de preços [skip ci]"
for i in 1 2 3 4; do  # tenta de novo se o push for recusado
  git pull -q --rebase origin "${GITHUB_REF_NAME:-main}" && git push -q origin "HEAD:${GITHUB_REF_NAME:-main}" && exit 0
  sleep $((i * 5))
done
echo "Não consegui enviar o histórico." >&2
