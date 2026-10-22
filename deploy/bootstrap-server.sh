#!/usr/bin/env bash
# One-time setup on a server that already runs Docker and nginx for other apps.
# Run as root:
#
#   sudo bash bootstrap-server.sh "<CI public key>"
#
# Idempotent. It only
#   1. checks Docker + the compose plugin are present (it never installs or reconfigures them)
#   2. creates a non-root `deploy` user in the docker group, authorised for the CI key
#   3. creates /opt/spotter owned by `deploy` (CI uploads the compose file there)
# It deliberately does NOT touch nginx, the firewall or other containers.
set -euo pipefail

PUBKEY="${1:?usage: bootstrap-server.sh \"<CI public key>\"}"
DEPLOY_USER=deploy
APP_DIR=/opt/spotter

if [[ $EUID -ne 0 ]]; then
  echo "error: run as root (sudo)" >&2
  exit 1
fi

echo "==> Docker"
if ! docker compose version > /dev/null 2>&1; then
  echo "error: Docker with the compose plugin is required (docker compose version failed)" >&2
  exit 1
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

PORT="${BACKEND_PORT:-8010}"
if ss -ltn 2> /dev/null | grep -q ":${PORT} "; then
  echo "warning: port ${PORT} is already in use on this host. Set BACKEND_PORT in ${APP_DIR}/.env" >&2
  echo "         and the proxy_pass port in the nginx site to a free port." >&2
fi

echo
echo "Done. Next: create ${APP_DIR}/.env as ${DEPLOY_USER} (see .env.example), add the nginx site"
echo "(deploy/nginx/spotter-api.conf) and run certbot, then push to master."
