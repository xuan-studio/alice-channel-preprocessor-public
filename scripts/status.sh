#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
[ ! -f .env ] || { set -a; . ./.env; set +a; }
docker compose --profile live --profile bot ps
printf '\n健康检查：'
curl -fsS "http://127.0.0.1:${WEB_PORT:-8848}/health" || true
printf '\n'
