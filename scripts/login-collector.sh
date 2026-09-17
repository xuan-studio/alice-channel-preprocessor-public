#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
[ -f .env ] || { echo "请先运行 ./scripts/setup.sh" >&2; exit 1; }
docker compose build
docker compose run --rm -it worker python -m app.login_collector
