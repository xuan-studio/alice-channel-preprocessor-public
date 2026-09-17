#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
umask 077
mkdir -p secrets backups

if [ -f .env ] && [ "${RESET_CONFIG:-0}" != "1" ]; then
  echo "已经存在 .env；为避免改错 TDLib 数据库口令，本次没有覆盖。" >&2
  echo "如确实要重新初始化，请先做好备份，再运行 RESET_CONFIG=1 ./scripts/setup.sh" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 Docker。请先安装 Docker Desktop（macOS/Windows）或 Docker Engine + Compose（Ubuntu）。" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "当前 Docker 没有 Compose 插件。请先安装 Docker Compose v2。" >&2
  exit 1
fi

prompt_visible() {
  label=$1
  default_value=${2:-}
  if [ -n "$default_value" ]; then
    printf '%s [%s]: ' "$label" "$default_value"
  else
    printf '%s: ' "$label"
  fi
  IFS= read -r answer
  if [ -z "$answer" ]; then answer=$default_value; fi
  REPLY=$answer
}

prompt_secret() {
  label=$1
  printf '%s: ' "$label"
  stty -echo
  IFS= read -r answer
  stty echo
  printf '\n'
  REPLY=$answer
}

save_secret() {
  name=$1
  value=$2
  printf '%s' "$value" > "secrets/$name"
  chmod 600 "secrets/$name"
}

prompt_visible "网页管理员用户名" "admin"
admin_user=$REPLY
case "$admin_user" in
  *[!A-Za-z0-9_-]*|'') echo "管理员用户名只能使用英文字母、数字、下划线和连字符。" >&2; exit 1;;
esac

while :; do
  prompt_secret "网页管理员密码（至少 16 位）"
  admin_password=$REPLY
  [ "${#admin_password}" -ge 16 ] && break
  echo "密码不足 16 位，请重试。"
done
save_secret platform_bootstrap_password "$admin_password"
unset admin_password

while :; do
  prompt_visible "Telegram API ID（纯数字）" ""
  telegram_api_id=$REPLY
  case "$telegram_api_id" in *[!0-9]*|'') echo "API ID 必须是纯数字。";; *) break;; esac
done
prompt_secret "Telegram API Hash"
[ -n "$REPLY" ] || { echo "API Hash 不能为空。" >&2; exit 1; }
save_secret telegram_api_hash "$REPLY"
unset REPLY

while :; do
  prompt_secret "TDLib 本地数据库口令（至少 8 位；以后启动必须使用同一个）"
  td_passphrase=$REPLY
  [ "${#td_passphrase}" -ge 8 ] && break
  echo "口令不足 8 位，请重试。"
done
save_secret telegram_database_passphrase "$td_passphrase"
unset td_passphrase

prompt_visible "是否启用 AI 总结？输入 y 或 n" "n"
ai_enabled=0
ai_base_url="https://api.openai.com/v1"
ai_model="gpt-4.1-mini"
if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
  prompt_visible "OpenAI-compatible BASE URL" "$ai_base_url"
  ai_base_url=$REPLY
  case "$ai_base_url" in http://*|https://*) ;; *) echo "BASE URL 必须以 http:// 或 https:// 开头。" >&2; exit 1;; esac
  prompt_visible "模型名" "$ai_model"
  ai_model=$REPLY
  case "$ai_model" in *[!A-Za-z0-9._:/-]*|'') echo "模型名包含不支持的字符。" >&2; exit 1;; esac
  prompt_secret "AI API Key"
  [ -n "$REPLY" ] || { echo "已取消 AI：Key 为空。"; }
  if [ -n "$REPLY" ]; then
    save_secret ai_api_key "$REPLY"
    ai_enabled=1
  fi
fi
[ -f secrets/ai_api_key ] || save_secret ai_api_key ""

prompt_visible "是否启用 Telegram 工作群 Bot？输入 y 或 n" "n"
bot_enabled=0
allowed_chats=""
if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
  prompt_secret "Telegram Bot Token"
  [ -n "$REPLY" ] || { echo "Bot Token 不能为空。" >&2; exit 1; }
  save_secret telegram_bot_token "$REPLY"
  prompt_visible "允许提交任务的群 chat ID（多个用逗号分隔）" ""
  allowed_chats=$REPLY
  case "$allowed_chats" in *[!0-9,-]*|'') echo "群 ID 必须是数字（多个用逗号分隔）。" >&2; exit 1;; esac
  bot_enabled=1
fi
[ -f secrets/telegram_bot_token ] || save_secret telegram_bot_token ""
[ -f secrets/database_password ] || save_secret database_password ""
[ -f secrets/site_proxy_token ] || save_secret site_proxy_token ""
[ -f secrets/telegram_standby_database_passphrase ] || save_secret telegram_standby_database_passphrase ""

cat > .env <<EOF
DEMO_MODE=0
TELEGRAM_WORKER_ENABLED=1
PUBLIC_BASE_URL=http://127.0.0.1:8848
TRUSTED_HOSTS=*
SESSION_COOKIE_SECURE=0
WEB_HOST=127.0.0.1
WEB_PORT=8848
PLATFORM_BOOTSTRAP_USERNAME=$admin_user
TELEGRAM_API_ID=$telegram_api_id
TELEGRAM_ACCOUNT_NAME=collector
BOT_ENABLED=$bot_enabled
TELEGRAM_WORKGROUP_CHAT_ID=0
TELEGRAM_ALLOWED_CHAT_IDS=$allowed_chats
TELEGRAM_ALLOWED_USER_IDS=
AI_ENABLED=$ai_enabled
AI_BASE_URL=$ai_base_url
AI_MODEL=$ai_model
TELEGRAM_REQUIRE_PROXY=0
TELEGRAM_PROXY_LINE_KEY=
TELEGRAM_PROXY_HOST=127.0.0.1
TELEGRAM_PROXY_PORT=0
TELEGRAM_PROXY_EXIT_FINGERPRINT=
RAW_RETENTION_DAYS=7
RESULT_RETENTION_DAYS=90
AUDIT_RETENTION_DAYS=365
SCAN_CACHE_HOURS=24
MAX_POSTS=100
MAX_PINNED=20
MAX_COMMENTS_PER_POST=1000
MAX_COMMENTS_PER_JOB=10000
MAX_NEW_JOINS_PER_DAY=20
EOF
chmod 600 .env

echo "配置已保存。下一步运行：./scripts/login-collector.sh"
