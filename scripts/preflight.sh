#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"

failed=0
for path in .env secrets/platform_bootstrap_password secrets/telegram_api_hash secrets/telegram_database_passphrase; do
  if [ ! -f "$path" ]; then
    echo "缺少：$path"
    failed=1
  fi
done
command -v docker >/dev/null 2>&1 || { echo "缺少 Docker"; failed=1; }
if command -v docker >/dev/null 2>&1; then
  docker compose version >/dev/null 2>&1 || { echo "缺少 Docker Compose v2"; failed=1; }
fi
[ "$failed" -eq 0 ] || exit 1
docker compose config --quiet
echo "预检通过：配置文件结构有效，敏感文件仅保存在本机 secrets/。"
