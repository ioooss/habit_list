#!/usr/bin/env bash
# 单机 staging 部署：发布已提交版本，签发/续期公网 IP 短证书，并保留上一 release。
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
REMOTE_DIR="${REMOTE_DIR:-/opt/inner-terrain-staging}"
SECRET_DIR="${STAGING_SECRET_DIR:-$REPO_ROOT/.secrets/staging}"
ENV_FILE="${APP_ENV_FILE:-$SECRET_DIR/.env.staging}"
HTPASSWD_FILE="${STAGING_HTPASSWD_FILE:-$SECRET_DIR/staging.htpasswd}"
ACCESS_FILE="${STAGING_ACCESS_FILE:-$SECRET_DIR/access.txt}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-$REPO_ROOT/.secrets/ssh/inner-terrain-deploy}"
BASE_COMPOSE_FILE="docker-compose.production.yml"
STAGING_COMPOSE_FILE="docker-compose.staging.yml"
CERTBOT_IMAGE="certbot/certbot:v5.4.0"
CERT_NAME="inner-terrain-staging-ip"
PRELOAD_IMAGES="${DEPLOY_PRELOAD_IMAGES:-0}"
PRELOAD_IMAGE_NAMES=(
  inner-terrain-backend:staging
  pgvector/pgvector:0.8.2-pg17-bookworm
  nginx:1.27-alpine
  "$CERTBOT_IMAGE"
)

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

read_secret_value() {
  local file="$1"
  local key="$2"
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ "$line" == "$key="* ]]; then
      printf '%s' "${line#*=}"
      return 0
    fi
  done < "$file"
  return 1
}

for command_name in curl docker git gzip scp ssh tar; do
  command -v "$command_name" >/dev/null 2>&1 || fail "missing command: $command_name"
done

[[ "${DEPLOY_CONFIRM:-}" == "inner-terrain-staging" ]] || \
  fail "set DEPLOY_CONFIRM=inner-terrain-staging to confirm a staging deployment"
[[ -n "$REPO_ROOT" && -d "$REPO_ROOT/.git" ]] || fail "project is not inside a Git worktree"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE; run deploy/prepare_staging_secrets.sh first"
[[ -f "$HTPASSWD_FILE" ]] || fail "missing $HTPASSWD_FILE"
[[ -f "$ACCESS_FILE" ]] || fail "missing $ACCESS_FILE"
[[ -f "$SSH_IDENTITY_FILE" ]] || fail "missing SSH identity file: $SSH_IDENTITY_FILE"
[[ "$SERVER" =~ ^[0-9A-Fa-f:.]+$ ]] || fail "SERVER_HOST must be an IP address"
[[ "$REMOTE_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || fail "invalid SERVER_USER"
[[ "$REMOTE_DIR" =~ ^/opt/[A-Za-z0-9._/-]+$ && "$REMOTE_DIR" != *".."* ]] || \
  fail "REMOTE_DIR must be a path below /opt without '..'"
[[ "$PRELOAD_IMAGES" == "0" || "$PRELOAD_IMAGES" == "1" ]] || \
  fail "DEPLOY_PRELOAD_IMAGES must be 0 or 1"

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -- habit_list_backend app.html)" ]]; then
  fail "habit_list_backend or app.html has uncommitted changes; commit the exact staging release first"
fi

access_username="$(read_secret_value "$ACCESS_FILE" USERNAME || true)"
access_password="$(read_secret_value "$ACCESS_FILE" PASSWORD || true)"
[[ "$access_username" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid staging access username"
[[ "$access_password" =~ ^[A-Fa-f0-9]{24}$ ]] || fail "invalid staging access password"

REVISION="$(git -C "$REPO_ROOT" rev-parse --verify HEAD)"
[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] || fail "could not resolve a full Git revision"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-${REVISION:0:12}"
RELEASE_DIR="$REMOTE_DIR/releases/$RELEASE_ID"
TMP_ROOT="$REPO_ROOT/.tmp/deploy"
mkdir -p -- "$TMP_ROOT"
ARCHIVE_PATH="$TMP_ROOT/inner-terrain-staging-$RELEASE_ID-$$.tar.gz"
WEB_PATH="$TMP_ROOT/inner-terrain-web-$RELEASE_ID-$$.html"
IMAGE_ARCHIVE_PATH="$TMP_ROOT/inner-terrain-images-$RELEASE_ID-$$.tar.gz"

cleanup() {
  case "$ARCHIVE_PATH" in
    "$TMP_ROOT"/*) [[ ! -f "$ARCHIVE_PATH" ]] || rm -f -- "$ARCHIVE_PATH" ;;
    *) printf 'warning: refusing to remove unexpected temporary path: %s\n' "$ARCHIVE_PATH" >&2 ;;
  esac
  case "$WEB_PATH" in
    "$TMP_ROOT"/*) [[ ! -f "$WEB_PATH" ]] || rm -f -- "$WEB_PATH" ;;
    *) printf 'warning: refusing to remove unexpected temporary path: %s\n' "$WEB_PATH" >&2 ;;
  esac
  case "$IMAGE_ARCHIVE_PATH" in
    "$TMP_ROOT"/*) [[ ! -f "$IMAGE_ARCHIVE_PATH" ]] || rm -f -- "$IMAGE_ARCHIVE_PATH" ;;
    *) printf 'warning: refusing to remove unexpected temporary path: %s\n' "$IMAGE_ARCHIVE_PATH" >&2 ;;
  esac
}
trap cleanup EXIT

git -C "$REPO_ROOT" archive \
  --format=tar.gz \
  --output="$ARCHIVE_PATH" \
  "$REVISION:habit_list_backend" \
  .dockerignore \
  .env.staging.example \
  Dockerfile \
  README.md \
  alembic.ini \
  app \
  deploy/nginx.staging.bootstrap.conf \
  deploy/nginx.staging.conf \
  docker-compose.production.yml \
  docker-compose.staging.yml \
  migrations \
  pyproject.toml
git -C "$REPO_ROOT" show "$REVISION:app.html" > "$WEB_PATH"

if ! ARCHIVE_CONTENTS="$(tar -tzf "$ARCHIVE_PATH")"; then
  fail "could not inspect the release archive"
fi
if grep -Eq \
  '(^|/)(tests|\.secrets|data)(/|$)|(^|/)\.env($|\.production$|\.staging$)' \
  <<<"$ARCHIVE_CONTENTS"; then
  fail "staging archive contains a forbidden development, data, or secret path"
fi
grep -qx 'docker-compose.staging.yml' <<<"$ARCHIVE_CONTENTS" || \
  fail "staging compose file is missing from the release archive"
[[ -s "$WEB_PATH" ]] || fail "could not export committed app.html"

if [[ "$PRELOAD_IMAGES" == "1" ]]; then
  for image_name in "${PRELOAD_IMAGE_NAMES[@]}"; do
    docker image inspect "$image_name" >/dev/null 2>&1 || \
      fail "missing local preload image: $image_name"
  done
fi

SSH_TARGET="$REMOTE_USER@$SERVER"
SSH_OPTIONS=(
  -i "$SSH_IDENTITY_FILE"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=10
)

printf '[1/8] Validate the staging Compose model locally...\n'
(
  cd "$PROJECT_DIR"
  APP_ENV_FILE="$ENV_FILE" \
    docker compose --env-file "$ENV_FILE" \
      -f "$BASE_COMPOSE_FILE" -f "$STAGING_COMPOSE_FILE" config --quiet
)

if [[ "${DEPLOY_DRY_RUN:-0}" == "1" ]]; then
  ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "set -eu; docker compose version >/dev/null; sudo -n true"
  printf 'Staging dry run passed for %s (%s); no release was created.\n' \
    "$RELEASE_ID" "$REVISION"
  exit 0
fi

if [[ "$PRELOAD_IMAGES" == "1" ]]; then
  printf 'Create a compressed, digest-preserving image bundle from local Docker storage...\n'
  docker save "${PRELOAD_IMAGE_NAMES[@]}" | gzip -1 > "$IMAGE_ARCHIVE_PATH"
  gzip -t "$IMAGE_ARCHIVE_PATH"
  [[ -s "$IMAGE_ARCHIVE_PATH" ]] || fail "local image bundle is empty"
fi

printf '[2/8] Prepare an immutable staging release directory...\n'
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; test ! -e '$RELEASE_DIR'; sudo install -d -o '$REMOTE_USER' -g '$REMOTE_USER' -m 0750 '$REMOTE_DIR' '$REMOTE_DIR/releases' '$RELEASE_DIR'; sudo install -d -m 0755 /var/www/certbot; sudo install -d -m 0700 /etc/letsencrypt /var/lib/letsencrypt; docker compose version >/dev/null"

printf '[3/8] Upload committed code, Web entry, and ignored staging configuration...\n'
scp "${SSH_OPTIONS[@]}" "$ARCHIVE_PATH" "$SSH_TARGET:$RELEASE_DIR/.bundle.tar.gz"
scp "${SSH_OPTIONS[@]}" "$WEB_PATH" "$SSH_TARGET:$RELEASE_DIR/.app.html.next"
scp "${SSH_OPTIONS[@]}" "$ENV_FILE" "$SSH_TARGET:$RELEASE_DIR/.env.staging.next"
scp "${SSH_OPTIONS[@]}" "$HTPASSWD_FILE" "$SSH_TARGET:$RELEASE_DIR/.staging.htpasswd.next"
if [[ "$PRELOAD_IMAGES" == "1" ]]; then
  scp "${SSH_OPTIONS[@]}" "$IMAGE_ARCHIVE_PATH" "$SSH_TARGET:$RELEASE_DIR/.images.tar.gz"
fi

printf '[4/8] Extract and validate the release on the server...\n'
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; cd '$RELEASE_DIR'; tar -xzf .bundle.tar.gz; install -d -m 0755 web; chmod 0644 .app.html.next; mv .app.html.next web/app.html; chmod 0600 .env.staging.next; mv .env.staging.next .env.staging; chmod 0644 .staging.htpasswd.next; mv .staging.htpasswd.next deploy/staging.htpasswd; rm -f -- .bundle.tar.gz; export APP_ENV_FILE=.env.staging; docker compose --env-file .env.staging -f '$BASE_COMPOSE_FILE' -f '$STAGING_COMPOSE_FILE' config --quiet"
if [[ "$PRELOAD_IMAGES" == "1" ]]; then
  ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "set -eu; cd '$RELEASE_DIR'; gzip -t .images.tar.gz; gzip -dc .images.tar.gz | docker load; rm -f -- .images.tar.gz; docker image inspect inner-terrain-backend:staging pgvector/pgvector:0.8.2-pg17-bookworm nginx:1.27-alpine '$CERTBOT_IMAGE' >/dev/null"
fi

printf '[5/8] Build and start the isolated staging stack behind an HTTP bootstrap gate...\n'
if [[ "$PRELOAD_IMAGES" == "1" ]]; then
  ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "set -eu; cd '$RELEASE_DIR'; export APP_ENV_FILE=.env.staging; docker image inspect inner-terrain-backend:staging >/dev/null; export STAGING_NGINX_TEMPLATE=./deploy/nginx.staging.bootstrap.conf; docker compose --env-file .env.staging -f '$BASE_COMPOSE_FILE' -f '$STAGING_COMPOSE_FILE' up -d --no-build --pull never --remove-orphans"
else
  ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "set -eu; cd '$RELEASE_DIR'; export APP_ENV_FILE=.env.staging; docker compose --env-file .env.staging -f '$BASE_COMPOSE_FILE' -f '$STAGING_COMPOSE_FILE' build app; export STAGING_NGINX_TEMPLATE=./deploy/nginx.staging.bootstrap.conf; docker compose --env-file .env.staging -f '$BASE_COMPOSE_FILE' -f '$STAGING_COMPOSE_FILE' up -d --no-build --remove-orphans"
fi

ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; for attempt in \$(seq 1 40); do if curl -fsS http://127.0.0.1:18780/ready >/dev/null; then exit 0; fi; if [ \"\$attempt\" -eq 40 ]; then cd '$RELEASE_DIR'; docker compose --env-file .env.staging -f '$BASE_COMPOSE_FILE' -f '$STAGING_COMPOSE_FILE' ps; exit 1; fi; sleep 3; done"

printf '[6/8] Verify the public HTTP-01 challenge path...\n'
challenge_name="codex-$RANDOM-$RANDOM"
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; printf '%s' '$challenge_name' | sudo tee '/var/www/certbot/$challenge_name' >/dev/null"
if ! challenge_body="$(curl -fsS --max-time 15 "http://$SERVER/.well-known/acme-challenge/$challenge_name")"; then
  ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "set -eu; sudo rm -f -- '/var/www/certbot/$challenge_name'"
  fail "port 80 or the Tencent firewall does not expose the ACME challenge path"
fi
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; sudo rm -f -- '/var/www/certbot/$challenge_name'"
[[ "$challenge_body" == "$challenge_name" ]] || fail "unexpected ACME challenge response"

cert_exists="$(ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "if sudo test -s '/etc/letsencrypt/live/$CERT_NAME/fullchain.pem'; then printf yes; else printf no; fi")"
if [[ "$cert_exists" != "yes" ]]; then
  [[ "${LETSENCRYPT_AGREE_TOS:-}" == "letsencrypt-subscriber-agreement" ]] || \
    fail "set LETSENCRYPT_AGREE_TOS=letsencrypt-subscriber-agreement after the account owner accepts the Let's Encrypt Subscriber Agreement"
  printf '[7/8] Request the trusted short-lived IP certificate...\n'
  ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "set -eu; sudo docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/lib/letsencrypt:/var/lib/letsencrypt -v /var/www/certbot:/var/www/certbot '$CERTBOT_IMAGE' certonly --non-interactive --agree-tos --register-unsafely-without-email --preferred-profile shortlived --webroot --webroot-path /var/www/certbot --cert-name '$CERT_NAME' --ip-address '$SERVER'"
else
  printf '[7/8] Reuse the existing staging IP certificate; renewal remains automatic.\n'
fi

printf '[8/8] Enable HTTPS, verify the protected entry, and mark the release current...\n'
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  "set -eu; cd '$RELEASE_DIR'; export APP_ENV_FILE=.env.staging; unset STAGING_NGINX_TEMPLATE; docker compose --env-file .env.staging -f '$BASE_COMPOSE_FILE' -f '$STAGING_COMPOSE_FILE' up -d --no-build --force-recreate nginx certbot-renew; for attempt in \$(seq 1 20); do if curl -fsS --resolve '$SERVER:443:127.0.0.1' --user '$access_username:$access_password' 'https://$SERVER/ready' >/dev/null; then break; fi; if [ \"\$attempt\" -eq 20 ]; then docker compose --env-file .env.staging -f '$BASE_COMPOSE_FILE' -f '$STAGING_COMPOSE_FILE' logs --tail=80 nginx; exit 1; fi; sleep 2; done; if [ -L '$REMOTE_DIR/current' ]; then previous=\$(readlink -f '$REMOTE_DIR/current'); case \"\$previous\" in '$REMOTE_DIR/releases/'*) ln -sfn \"\$previous\" '$REMOTE_DIR/previous.next'; mv -Tf '$REMOTE_DIR/previous.next' '$REMOTE_DIR/previous' ;; esac; fi; ln -sfn '$RELEASE_DIR' '$REMOTE_DIR/current.next'; mv -Tf '$REMOTE_DIR/current.next' '$REMOTE_DIR/current'; docker compose --env-file .env.staging -f '$BASE_COMPOSE_FILE' -f '$STAGING_COMPOSE_FILE' ps"

curl --fail --silent --show-error --max-time 20 \
  --config <(printf 'user = "%s:%s"\nurl = "https://%s/ready"\n' \
    "$access_username" "$access_password" "$SERVER") >/dev/null

printf 'Staging deployment completed: https://%s (%s). Credentials remain in %s.\n' \
  "$SERVER" "$REVISION" "$ACCESS_FILE"
