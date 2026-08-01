#!/usr/bin/env bash
# 生成 staging 专用配置与访问凭据；不会读取或改写生产配置。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
STAGING_HOST="${STAGING_HOST:-81.70.177.186}"
SECRET_DIR="$REPO_ROOT/.secrets/staging"
ENV_FILE="$SECRET_DIR/.env.staging"
HTPASSWD_FILE="$SECRET_DIR/staging.htpasswd"
ACCESS_FILE="$SECRET_DIR/access.txt"

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

[[ -n "$REPO_ROOT" && -d "$REPO_ROOT/.git" ]] || fail "project is not inside a Git worktree"
[[ "$STAGING_HOST" =~ ^[0-9A-Fa-f:.]+$ ]] || fail "STAGING_HOST must be an IP address"
command -v openssl >/dev/null 2>&1 || fail "missing command: openssl"

if [[ -e "$ENV_FILE" || -e "$HTPASSWD_FILE" || -e "$ACCESS_FILE" ]]; then
  [[ -f "$ENV_FILE" && -f "$HTPASSWD_FILE" && -f "$ACCESS_FILE" ]] || \
    fail "staging secret set is incomplete; inspect $SECRET_DIR instead of overwriting it"
  printf 'Staging secrets already exist at %s; kept unchanged.\n' "$SECRET_DIR"
  exit 0
fi

read_env_value() {
  local file="$1"
  local key="$2"
  local line value
  [[ -f "$file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ "$line" == "$key="* ]]; then
      value="${line#*=}"
      if [[ "$value" == \"*\" && "$value" == *\" ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
        value="${value:1:${#value}-2}"
      fi
      [[ -n "$value" ]] || return 1
      printf '%s' "$value"
      return 0
    fi
  done < "$file"
  return 1
}

dashscope_api_key="$(read_env_value "$PROJECT_DIR/.env" DASHSCOPE_API_KEY || true)"
if [[ -z "$dashscope_api_key" ]]; then
  dashscope_api_key="$(read_env_value "$REPO_ROOT/.env" DASHSCOPE_API_KEY || true)"
fi
[[ -n "$dashscope_api_key" ]] || \
  fail "DASHSCOPE_API_KEY was not found in habit_list_backend/.env or repository .env"
[[ "$dashscope_api_key" != *$'\n'* && "$dashscope_api_key" != *$'\r'* ]] || \
  fail "DASHSCOPE_API_KEY contains an unexpected newline"

random_hex() {
  openssl rand -hex "$1" | tr -d '\r\n'
}

fernet_key() {
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '\r\n'
}

umask 077
mkdir -p -- "$SECRET_DIR"

database_password="$(random_hex 24)"
api_auth_token="stg_$(random_hex 32)"
admin_token="stg_admin_$(random_hex 32)"
auth_token_pepper="$(random_hex 32)"
pii_encryption_key="$(fernet_key)"
admin_mfa_encryption_key="$(fernet_key)"
access_username="terrain-test"
access_password="$(random_hex 12)"
htpasswd_hash="$(printf '%s' "$access_password" | openssl passwd -apr1 -stdin)"

cat > "$ENV_FILE" <<EOF
APP_ENV=staging
LOG_LEVEL=INFO
API_PREFIX=/api/v1
CORS_ALLOWED_ORIGINS=https://$STAGING_HOST
PROCESS_ROLE=api

DATABASE_SCHEMA_MODE=alembic
DATABASE_URL=postgresql+psycopg://terrain_staging:$database_password@postgres:5432/terrain_staging
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
DATABASE_POOL_TIMEOUT_SECONDS=30
DATABASE_POOL_RECYCLE_SECONDS=1800
POSTGRES_DB=terrain_staging
POSTGRES_USER=terrain_staging
POSTGRES_PASSWORD=$database_password

AUTH_MODE=legacy
API_AUTH_TOKEN=$api_auth_token
ADMIN_TOKEN=$admin_token
AUTH_TOKEN_PEPPER=$auth_token_pepper
PII_ENCRYPTION_KEY=$pii_encryption_key
ADMIN_MFA_ENCRYPTION_KEY=$admin_mfa_encryption_key
DEFAULT_USER_ID=01930000-0000-0000-0000-000000000001
DEFAULT_USER_LOCALE=zh-CN
DEFAULT_USER_TIMEZONE=Asia/Shanghai

DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_API_KEY=$dashscope_api_key
DASHSCOPE_LLM_MODEL=qwen-plus
DASHSCOPE_EMBEDDING_MODEL=qwen3.7-text-embedding
DASHSCOPE_EMBEDDING_DIM=1024
DASHSCOPE_TIMEOUT_SEC=120
DASHSCOPE_MAX_RETRY=2
RPM_LIMIT_PER_MIN=30

MEMORY_V2_MODE=shadow_write
MEMORY_V2_EXTRACTOR_MODE=rules
MEMORY_V2_POLICY_VERSION=terrain-memory-v1
MEMORY_V2_EXTRACTOR_VERSION=terrain-extractor-v1
MEMORY_V2_RETRIEVAL_TOPK=2
MEMORY_V2_CANDIDATE_LIMIT=200
MEMORY_V2_MIN_RETRIEVAL_SCORE=0.30
MEMORY_V2_AUTO_CONFIRM_CONF=0.86
MEMORY_V2_WORKER_INTERVAL_SECONDS=15
MEMORY_V2_OUTBOX_BATCH_SIZE=20
MEMORY_V2_OUTBOX_MAX_ATTEMPTS=5
MEMORY_V2_EMBEDDING_ENABLED=false

WORKER_HEARTBEAT_PATH=/tmp/habit-list-worker-heartbeat.json
WORKER_HEARTBEAT_INTERVAL_SECONDS=10
WORKER_HEARTBEAT_STALE_SECONDS=45
DEPLOY_DOMAIN=$STAGING_HOST
APP_HOST_PORT=18780
EOF

printf '%s:%s\n' "$access_username" "$htpasswd_hash" > "$HTPASSWD_FILE"
cat > "$ACCESS_FILE" <<EOF
URL=https://$STAGING_HOST
USERNAME=$access_username
PASSWORD=$access_password
EOF

chmod 0600 "$ENV_FILE" "$HTPASSWD_FILE" "$ACCESS_FILE" 2>/dev/null || true
printf 'Created isolated staging secrets in %s. Values were not printed.\n' "$SECRET_DIR"
