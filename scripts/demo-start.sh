#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
umask 077

if [ -f .env ]; then
  echo "已经存在 .env；为避免覆盖真实配置，演示启动已停止。" >&2
  echo "请在新的源码目录运行，或确认无用后自行移走现有 .env。" >&2
  exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "缺少 Docker。" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "缺少 Docker Compose v2。" >&2; exit 1; }

cp .env.demo .env
mkdir -p secrets backups exports
password="Demo-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
printf '%s' "$password" > secrets/platform_bootstrap_password
printf '%s' 'demo-session-disabled' > secrets/telegram_database_passphrase
: > secrets/telegram_api_hash
: > secrets/ai_api_key
: > secrets/telegram_bot_token
: > secrets/database_password
: > secrets/site_proxy_token
: > secrets/telegram_standby_database_passphrase
chmod 600 .env secrets/platform_bootstrap_password secrets/telegram_database_passphrase secrets/telegram_api_hash secrets/ai_api_key secrets/telegram_bot_token secrets/database_password secrets/site_proxy_token secrets/telegram_standby_database_passphrase

docker compose up -d --build web
attempt=0
until curl -fsS http://127.0.0.1:8858/health >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || { echo "演示服务未在预期时间内就绪。" >&2; exit 1; }
  sleep 2
done
docker compose exec -T web python -m app.demo_seed

printf '\n演示服务：http://127.0.0.1:8858\n'
printf '用户名：demo_admin\n'
printf '密码：%s\n' "$password"
printf '提示：密码只在本次初始化时显示；演示服务仅绑定 127.0.0.1。\n'
