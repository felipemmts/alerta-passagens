#!/usr/bin/env bash
# Guarda os arquivos de estado (histórico, consultas, fila e log) no branch "dados",
# fora do histórico da main. O branch tem sempre um commit só, substituído a cada
# rodada: não acumula commits nem dá conflito.
set -u
ramo_dados="dados"
arquivos="historico.json consultas.json progresso.json ultima_execucao.log"
touch $arquivos
for f in $arquivos; do [ -s "$f" ] || [ "$f" = ultima_execucao.log ] || echo '{}' > "$f"; done

indice=$(mktemp)
rm -f "$indice"
GIT_INDEX_FILE="$indice" git add -f $arquivos
arvore=$(GIT_INDEX_FILE="$indice" git write-tree)
rm -f "$indice"

# nada mudou desde o último salvamento?
anterior=$(git rev-parse -q --verify "refs/remotes/origin/$ramo_dados^{tree}" 2>/dev/null || true)
if [ "$arvore" = "$anterior" ]; then
  echo "Estado sem alterações."
  exit 0
fi

commit=$(git -c user.name="github-actions[bot]" \
             -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
             commit-tree "$arvore" -m "Estado do alerta de passagens")
for i in 1 2 3 4; do
  if git push -q --force origin "$commit:refs/heads/$ramo_dados"; then
    git update-ref "refs/remotes/origin/$ramo_dados" "$commit"
    exit 0
  fi
  sleep $((i * 5))
done
echo "Não consegui salvar o estado no branch $ramo_dados." >&2
exit 0
