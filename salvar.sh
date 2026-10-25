#!/usr/bin/env bash
# Envia os arquivos de estado (histórico, consultas, fila e log) para o repositório.
# O estado desta execução é sempre o mais novo: em vez de mesclar com o que está
# no GitHub (o que pode dar conflito e travar o salvamento), parte da versão mais
# recente do repositório e grava os arquivos daqui por cima.
set -u
ramo="${GITHUB_REF_NAME:-main}"
arquivos="historico.json consultas.json progresso.json ultima_execucao.log"
touch progresso.json ultima_execucao.log
copia=$(mktemp -d)
cp $arquivos "$copia"/
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git rebase --abort >/dev/null 2>&1 || true   # limpa algum salvamento anterior que tenha ficado pela metade
for i in 1 2 3 4; do
  git fetch -q origin "$ramo" && git reset -q --hard "origin/$ramo" || { sleep $((i * 5)); continue; }
  cp "$copia"/* .
  git add $arquivos
  if git diff --cached --quiet; then
    echo "Histórico sem alterações."
    exit 0
  fi
  git commit -q -m "Atualiza histórico e consultas de preços [skip ci]"
  git push -q origin "HEAD:$ramo" && exit 0
  sleep $((i * 5))
done
echo "Não consegui enviar o histórico." >&2
exit 0
