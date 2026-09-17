#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
[ -f .env ] || { echo "请先运行 ./scripts/setup.sh" >&2; exit 1; }
set -a
. ./.env
set +a
if [ -s secrets/telegram_bot_token ]; then
  docker compose --profile live --profile bot up -d --build
else
  docker compose --profile live up -d --build
fi
echo "服务已启动。浏览器打开：${PUBLIC_BASE_URL:-http://127.0.0.1:8848}"
