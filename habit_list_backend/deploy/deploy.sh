#!/usr/bin/env bash
# 单机生产部署入口：只发布已提交代码，并为每次发布保留独立版本目录。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
case "$REPO_ROOT" in
  [A-Za-z]:/*)
    if command -v cygpath >/dev/null 2>&1; then
      REPO_ROOT="$(cygpath -u "$REPO_ROOT")"
    elif command -v wslpath >/dev/null 2>&1; then
      REPO_ROOT="$(wslpath -u "$REPO_ROOT")"
    fi
    ;;
esac
SERVER="${SERVER_HOST:-81.70.177.186}"
REMOTE_USER="${SERVER_USER:-ubuntu}"
REMOTE_DIR="${REMOTE_DIR:-/opt/habit_list_backend}"
ENV_FILE="${APP_ENV_FILE:-$PROJECT_DIR/.env.production}"
COMPOSE_FILE="docker-compose.production.yml"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-$REPO_ROOT/.secrets/ssh/inner-terrain-deploy}"

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

for command_name in docker git scp ssh tar; do
  command -v "$command_name" >/dev/null 2>&1 || fail "missing command: $command_name"
done

[[ "${DEPLOY_CONFIRM:-}" == "inner-terrain-production" ]] || \
  fail "set DEPLOY_CONFIRM=inner-terrain-production to confirm a production deployment"
[[ -n "$REPO_ROOT" && -d "$REPO_ROOT/.git" ]] || fail "project is not inside a Git worktree"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE; copy .env.production.example and fill real secrets"
[[ -f "$SSH_IDENTITY_FILE" ]] || fail "missing SSH identity file: $SSH_IDENTITY_FILE"
[[ "$SERVER" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid SERVER_HOST"
[[ "$REMOTE_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || fail "invalid SERVER_USER"
[[ "$REMOTE_DIR" =~ ^/opt/[A-Za-z0-9._/-]+$ && "$REMOTE_DIR" != *".."* ]] || \
  fail "REMOTE_DIR must be a path below /opt without '..'"

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -- habit_list_backend)" ]]; then
  fail "habit_list_backend has uncommitted changes; commit and verify the exact release first"
fi

REVISION="$(git -C "$REPO_ROOT" rev-parse --verify HEAD)"
[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] || fail "could not resolve a full Git revision"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-${REVISION:0:12}"
RELEASE_DIR="$REMOTE_DIR/releases/$RELEASE_ID"
TMP_ROOT="$REPO_ROOT/.tmp/deploy"
mkdir -p -- "$TMP_ROOT"
ARCHIVE_PATH="$TMP_ROOT/habit-list-$RELEASE_ID-$$.tar.gz"

cleanup() {
  case "$ARCHIVE_PATH" in
    "$TMP_ROOT"/*) [[ ! -f "$ARCHIVE_PATH" ]] || rm -f -- "$ARCHIVE_PATH" ;;
    *) printf 'warning: refusing to remove unexpected temporary path: %s\n' "$ARCHIVE_PATH" >&2 ;;
  esac
}
trap cleanup EXIT

git -C "$REPO_ROOT" archive \
  --format=tar.gz \
  --output="$ARCHIVE_PATH" \
  "$REVISION:habit_list_backend"
if ! ARCHIVE_CONTENTS="$(tar -tzf "$ARCHIVE_PATH")"; then
  fail "could not inspect the release archive"
fi
if grep -Eq \
  '(^|/)(tests|\.secrets)(/|$)|(^|/)\.env$|(^|/)\.env\.production$|(^|/)\.env\.integration\.example$' \
  <<<"$ARCHIVE_CONTENTS"; then
  fail "release archive contains a forbidden development or secret path"
fi

SSH_TARGET="$REMOTE_USER@$SERVER"
SSH_OPTIONS=(
  -i "$SSH_IDENTITY_FILE"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=10
)

printf '[1/6] Validate the production Compose model locally...\n'
(
  cd "$PROJECT_DIR"
  APP_ENV_FILE="$ENV_FILE" \
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet
)

if [[ "${DEPLOY_DRY_RUN:-0}" == "1" ]]; then
  ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "set -eu; docker compose version >/dev/null; test -d '$REMOTE_DIR'"
  printf 'Dry run passed for release %s (%s); no remote release was created.\n' \
    "$RELEASE_ID" "$REVISION"
  exit 0
fi

printf '[2/6] Verify the dedicated SSH key and prepare an immutable release directory...\n'
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; test ! -e '$RELEASE_DIR'; install -d -m 0750 '$REMOTE_DIR' '$REMOTE_DIR/releases' '$RELEASE_DIR'; docker compose version >/dev/null"

printf '[3/6] Upload the committed source archive and production configuration...\n'
scp "${SSH_OPTIONS[@]}" "$ARCHIVE_PATH" "$SSH_TARGET:$RELEASE_DIR/.bundle.tar.gz"
scp "${SSH_OPTIONS[@]}" "$ENV_FILE" "$SSH_TARGET:$RELEASE_DIR/.env.production.next"

printf '[4/6] Extract and validate the release on the server...\n'
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; cd '$RELEASE_DIR'; tar -xzf .bundle.tar.gz; test -f '$COMPOSE_FILE'; chmod 0600 .env.production.next; mv .env.production.next .env.production; rm -f -- .bundle.tar.gz; export APP_ENV_FILE=.env.production; docker compose --env-file .env.production -f '$COMPOSE_FILE' config --quiet"

printf '[5/6] Migrate and start the production services...\n'
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; cd '$RELEASE_DIR'; export APP_ENV_FILE=.env.production; docker compose --env-file .env.production -f '$COMPOSE_FILE' build app; docker compose --env-file .env.production -f '$COMPOSE_FILE' up -d --no-build --remove-orphans"

printf '[6/6] Wait for readiness, then atomically mark this release current...\n'
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; cd '$RELEASE_DIR'; for attempt in \$(seq 1 30); do if curl -fsS http://127.0.0.1:8780/ready >/dev/null; then break; fi; if [ \"\$attempt\" -eq 30 ]; then docker compose --env-file .env.production -f '$COMPOSE_FILE' ps; exit 1; fi; sleep 3; done; if [ -L '$REMOTE_DIR/current' ]; then previous=\$(readlink -f '$REMOTE_DIR/current'); case \"\$previous\" in '$REMOTE_DIR/releases/'*) ln -sfn \"\$previous\" '$REMOTE_DIR/previous.next'; mv -Tf '$REMOTE_DIR/previous.next' '$REMOTE_DIR/previous' ;; esac; fi; ln -sfn '$RELEASE_DIR' '$REMOTE_DIR/current.next'; mv -Tf '$REMOTE_DIR/current.next' '$REMOTE_DIR/current'; curl -fsS http://127.0.0.1:8780/ready; printf '\n'; docker compose --env-file .env.production -f '$COMPOSE_FILE' ps"

printf 'Production deployment completed: %s (%s). Verify HTTPS before opening public traffic.\n' "$RELEASE_ID" "$REVISION"
