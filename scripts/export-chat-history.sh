#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"

output_dir="$root/exports/chat-history"
mkdir -p "$output_dir"

if [ -t 0 ]; then
  tty_args="-it"
else
  tty_args="-T"
fi

docker compose run --rm $tty_args \
  -v "$output_dir:/app/local-exports" \
  -e HISTORY_EXPORT_OUTPUT_DIR=/app/local-exports \
  worker python -m app.chat_history_export "$@"

printf '\n导出目录：%s\n' "$output_dir"
