#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
mkdir -p backups
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T web sqlite3 /app/data/preprocessor.db ".backup '/app/data/preprocessor-backup.db'"
docker compose cp web:/app/data/preprocessor-backup.db "backups/preprocessor-$timestamp.db"
docker compose exec -T web rm -f /app/data/preprocessor-backup.db
find backups -type f -name 'preprocessor-*.db' -mtime +14 -delete
echo "备份完成：backups/preprocessor-$timestamp.db"
