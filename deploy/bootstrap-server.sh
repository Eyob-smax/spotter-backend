#!/usr/bin/env bash
# One-time server setup (Ubuntu 22.04/24.04). Run as root on the VPS:
#
#   sudo bash bootstrap-server.sh "<CI public key>" [--stop-nginx]
#
# Idempotent: safe to run again. It
#   1. installs Docker Engine + compose plugin (if missing)
#   2. creates a non-root `deploy` user in the docker group, authorised for the CI key
#   3. creates /opt/spotter owned by `deploy` (CI uploads compose files there)
#   4. allows SSH, HTTP and HTTPS through ufw
#   5. with --stop-nginx: stops and disables a system nginx holding ports 80/443
#      (Caddy needs them). Only use this if nginx serves nothing else.
set -euo pipefail

PUBKEY="${1:?usage: bootstrap-server.sh \"<CI public key>\" [--stop-nginx]}"
STOP_NGINX="${2:-}"
DEPLOY_USER=deploy
APP_DIR=/opt/spotter

if [[ $EUID -ne 0 ]]; then
  echo "error: run as root (sudo)" >&2
  exit 1
fi

echo "==> Docker"
if ! command -v docker > /dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version

echo "==> User '${DEPLOY_USER}'"
id "$DEPLOY_USER" > /dev/null 2>&1 || adduser --disabled-password --gecos "" "$DEPLOY_USER"
usermod -aG docker "$DEPLOY_USER"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/${DEPLOY_USER}/.ssh"
AUTH="/home/${DEPLOY_USER}/.ssh/authorized_keys"
touch "$AUTH"
grep -qxF "$PUBKEY" "$AUTH" || echo "$PUBKEY" >> "$AUTH"
chown "$DEPLOY_USER:$DEPLOY_USER" "$AUTH"
chmod 600 "$AUTH"

echo "==> App directory ${APP_DIR}"
install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR" "$APP_DIR/deploy"

echo "==> Firewall"
if command -v ufw > /dev/null; then
  ufw allow OpenSSH > /dev/null
  ufw allow 80/tcp > /dev/null
  ufw allow 443/tcp > /dev/null
  ufw allow 443/udp > /dev/null
  ufw --force enable > /dev/null
  ufw status | head -12
fi

if [[ "$STOP_NGINX" == "--stop-nginx" ]] && systemctl is-active --quiet nginx; then
  echo "==> Stopping system nginx (Caddy will serve 80/443)"
  systemctl disable --now nginx
fi

if ss -ltnp 2> /dev/null | grep -qE ':(80|443)\s'; then
  echo "warning: something is still listening on port 80/443:" >&2
  ss -ltnp | grep -E ':(80|443)\s' >&2 || true
  echo "Caddy will not start until those ports are free." >&2
fi

echo
echo "Done. Next: create ${APP_DIR}/.env as ${DEPLOY_USER} (see .env.example), then push to master."
