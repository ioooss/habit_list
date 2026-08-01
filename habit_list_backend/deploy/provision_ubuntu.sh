#!/usr/bin/env bash
# Ubuntu 单机生产主机初始化：Docker Engine、Compose、Certbot 与部署目录。
set -euo pipefail

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

[[ -r /etc/os-release ]] || fail "missing /etc/os-release"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || fail "this provisioner only supports Ubuntu"
CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
ARCHITECTURE="$(dpkg --print-architecture)"
[[ "$CODENAME" =~ ^[a-z0-9]+$ ]] || fail "invalid Ubuntu codename"
[[ "$ARCHITECTURE" =~ ^[a-z0-9]+$ ]] || fail "invalid dpkg architecture"
sudo -n true || fail "passwordless sudo is required for non-interactive provisioning"

for package_name in docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc; do
  if dpkg-query -W -f='${db:Status-Abbrev}' "$package_name" 2>/dev/null | grep -q '^ii'; then
    fail "conflicting package is installed: $package_name; inspect it before provisioning"
  fi
done

# Do not perform a broad OS upgrade during application provisioning.
sudo env DEBIAN_FRONTEND=noninteractive apt-get \
  -o Acquire::Retries=3 -o Acquire::ForceIPv4=true update
sudo env DEBIAN_FRONTEND=noninteractive apt-get \
  -o Acquire::Retries=3 -o Acquire::ForceIPv4=true \
  install -y --no-install-recommends ca-certificates curl rsync certbot

# Docker's official apt repository (idempotent, no remote convenience script).
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -4 -fsSL \
  --retry 5 --retry-all-errors --connect-timeout 10 \
  https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
printf '%s\n' \
  'Types: deb' \
  'URIs: https://download.docker.com/linux/ubuntu' \
  "Suites: $CODENAME" \
  'Components: stable' \
  "Architectures: $ARCHITECTURE" \
  'Signed-By: /etc/apt/keyrings/docker.asc' | \
  sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null

sudo env DEBIAN_FRONTEND=noninteractive apt-get \
  -o Acquire::Retries=3 -o Acquire::ForceIPv4=true update
sudo env DEBIAN_FRONTEND=noninteractive apt-get \
  -o Acquire::Retries=3 -o Acquire::ForceIPv4=true \
  install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

# The application reverse proxy runs in Docker. Certbot uses webroot mode and
# therefore does not install or control a second host-level Nginx process.
sudo install -d -m 0750 -o "$USER" -g "$(id -gn)" /opt/habit_list_backend
sudo install -d -m 0755 -o root -g root /var/www/certbot

sudo docker version --format 'server={{.Server.Version}}'
sudo docker compose version
printf 'Provisioning completed. Start a new SSH login before running deploy/deploy.sh.\n'
