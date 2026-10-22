#!/usr/bin/env bash
# Runs ON THE SERVER (invoked over SSH by the GitHub Actions deploy job).
#
#   remote-deploy.sh <image>      e.g. ghcr.io/owner/spotter-backend:sha-1a2b3c4
#
# Pulls the image, restarts the stack and waits for every container's health
# check. If the new version doesn't become healthy, the previously deployed
# image is restored and the script exits non-zero so the workflow fails.
set -euo pipefail

IMAGE="${1:?usage: remote-deploy.sh <image>}"
cd "$(dirname "$0")/.."

export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-spotter}"
STATE_FILE=.deployed-image
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"

if [[ ! -f .env ]]; then
  echo "error: $(pwd)/.env is missing; create it from .env.example first" >&2
  exit 1
fi

previous="$(cat "$STATE_FILE" 2>/dev/null || true)"
echo "Deploying ${IMAGE} (previous: ${previous:-none})"

export BACKEND_IMAGE="$IMAGE"
docker compose pull --quiet backend

if docker compose up -d --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT"; then
  echo "$IMAGE" > "$STATE_FILE"
  docker image prune -f --filter "until=168h" > /dev/null
  echo "Deployed ${IMAGE}"
  exit 0
fi

echo "error: ${IMAGE} did not become healthy" >&2
docker compose logs --tail 80 backend >&2 || true

if [[ -n "$previous" && "$previous" != "$IMAGE" ]]; then
  echo "Rolling back to ${previous}" >&2
  export BACKEND_IMAGE="$previous"
  docker compose up -d --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT" \
    && echo "Rolled back to ${previous}" >&2 \
    || echo "error: rollback to ${previous} also failed" >&2
fi
exit 1
