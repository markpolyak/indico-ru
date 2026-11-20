#!/bin/bash
# Локальная сборка образа indico-ru (linux/amd64). Запускать из любого места.
# Токен Transifex (необязательно): ~/.indico-tx-token
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${1:-3.3.14}"
TAG="indico-ru:${VERSION}"
# Источник плагина autoaccept: локальный клон или git-URL, напр.
#   AUTOACCEPT_SRC=https://github.com/markpolyak/indico-plugin-autoaccept.git#v2.1.0
AUTOACCEPT_SRC="${AUTOACCEPT_SRC:-https://github.com/markpolyak/indico-plugin-autoaccept.git#v2.1.0}"
# На Docker Desktop используем его builder (с кешем), если он есть
BUILDER="${BUILDER-$(docker buildx inspect desktop-linux >/dev/null 2>&1 && echo desktop-linux)}"
SECRET_ARGS=()
# TX_REFRESH=0 — не скачивать перевод заново (взять из кеша); полезно, если менялся только плагин.
# Осторожно: берётся кеш прошлой сборки с TX_REFRESH=0, а он может быть сколь угодно старым —
# смотрите в логе «из слоя применено N строк; нет в каталоге: M» (M > 0 — каталог устарел).
if [ -s "$HOME/.indico-tx-token" ]; then
    SECRET_ARGS=(--secret id=tx_token,src="$HOME/.indico-tx-token")
    [ "${TX_REFRESH:-1}" != 0 ] && SECRET_ARGS+=(--build-arg TX_PULL_ID="$(date +%s)")
fi
docker buildx build ${BUILDER:+--builder "$BUILDER"} --platform linux/amd64 -f Dockerfile \
    --build-arg INDICO_VERSION="$VERSION" ${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"} \
    --build-context autoaccept="$AUTOACCEPT_SRC" \
    -t "$TAG" --load --progress=plain . 2>&1 | tee "build-${VERSION}.log"
echo "Готово: $TAG"
