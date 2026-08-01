#!/usr/bin/env bash
# 单机生产部署入口。不会删除远端文件，也不会同步开发 .env。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SERVER="${SERVER_HOST:-81.70.177.186}"
REMOTE_USER="${SERVER_USER:-ubuntu}"
REMOTE_DIR="${REMOTE_DIR:-/opt/habit_list_backend}"
ENV_FILE="$PROJECT_DIR/.env.production"
COMPOSE_FILE="docker-compose.production.yml"

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

for command_name in docker rsync scp ssh; do
  command -v "$command_name" >/dev/null 2>&1 || fail "missing command: $command_name"
done

[[ "${DEPLOY_CONFIRM:-}" == "inner-terrain-production" ]] || \
  fail "set DEPLOY_CONFIRM=inner-terrain-production to confirm a production deployment"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE; copy .env.production.example and fill real secrets"
[[ "$SERVER" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid SERVER_HOST"
[[ "$REMOTE_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || fail "invalid SERVER_USER"
[[ "$REMOTE_DIR" =~ ^/opt/[A-Za-z0-9._/-]+$ && "$REMOTE_DIR" != *".."* ]] || \
  fail "REMOTE_DIR must be a path below /opt without '..'"

SSH_TARGET="$REMOTE_USER@$SERVER"

printf '[1/5] Validate the production Compose model locally...\n'
(
  cd "$PROJECT_DIR"
  APP_ENV_FILE=.env.production \
    docker compose --env-file .env.production -f "$COMPOSE_FILE" config --quiet
)

printf '[2/5] Prepare the remote directory and synchronize code (no remote delete)...\n'
ssh "$SSH_TARGET" "install -d -m 0750 '$REMOTE_DIR' '$REMOTE_DIR/.env-backups'"
rsync -az \
  --exclude='.git/' \
  --exclude='.conda/' \
  --exclude='.venv/' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  "$PROJECT_DIR/" "$SSH_TARGET:$REMOTE_DIR/"

printf '[3/5] Upload production configuration atomically and retain the previous copy...\n'
scp "$ENV_FILE" "$SSH_TARGET:$REMOTE_DIR/.env.production.next"
ssh "$SSH_TARGET" "set -e; cd '$REMOTE_DIR'; chmod 600 .env.production.next; if [ -f .env.production ]; then stamp=\$(date -u +%Y%m%dT%H%M%SZ); cp -p .env.production \".env-backups/.env.production.\$stamp\"; fi; mv .env.production.next .env.production"

printf '[4/5] Validate, migrate, and start the production services...\n'
ssh "$SSH_TARGET" "set -e; cd '$REMOTE_DIR'; export APP_ENV_FILE=.env.production; docker compose --env-file .env.production -f '$COMPOSE_FILE' config --quiet; docker compose --env-file .env.production -f '$COMPOSE_FILE' up -d --build --remove-orphans"

printf '[5/5] Wait for API readiness and show service state...\n'
ssh "$SSH_TARGET" "set -e; cd '$REMOTE_DIR'; for attempt in \$(seq 1 20); do if curl -fsS http://127.0.0.1:8780/ready >/dev/null; then break; fi; if [ \"\$attempt\" -eq 20 ]; then docker compose --env-file .env.production -f '$COMPOSE_FILE' ps; exit 1; fi; sleep 3; done; curl -fsS http://127.0.0.1:8780/ready; printf '\n'; docker compose --env-file .env.production -f '$COMPOSE_FILE' ps"

printf 'Production deployment completed. Verify the public /ready endpoint over HTTPS after TLS is configured.\n'
