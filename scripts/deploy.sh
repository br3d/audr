#!/usr/bin/env bash
# Deploy the application to the remote host via SSH.
# Pulls the latest images from the registry, runs migrations, restarts services.
# Usage: ./scripts/deploy.sh
# Requires: SSH key at ./id_ed25519 (mode 600) or AUDR_SSH_KEY env var.
# Exits non-zero on any failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

DEPLOY_HOST="${DEPLOY_HOST:-codex@192.168.1.228}"
SSH_KEY="${AUDR_SSH_KEY:-${ROOT}/id_ed25519}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
REMOTE_DIR="${REMOTE_DIR:-/opt/audr}"

echo "==> Deploying to ${DEPLOY_HOST}:${REMOTE_DIR}"

# Copy compose file to the remote host
scp -i "${SSH_KEY}" -o StrictHostKeyChecking=accept-new \
  "${ROOT}/compose.yaml" "${DEPLOY_HOST}:${REMOTE_DIR}/compose.yaml"

# Pull new images, run migrations, restart services
$SSH "${DEPLOY_HOST}" bash -s -- "${REMOTE_DIR}" <<'REMOTE'
set -euo pipefail
REMOTE_DIR="$1"
cd "${REMOTE_DIR}"

echo "  -> Pulling latest images"
docker compose pull init api web worker

echo "  -> Running migrations"
docker compose run --rm migrate

echo "  -> Restarting services"
docker compose up -d --remove-orphans

echo "  -> Waiting for API health check"
for i in $(seq 1 30); do
  if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
    echo "  -> API is healthy (attempt ${i})"
    exit 0
  fi
  sleep 2
done
echo "  -> ERROR: API did not become healthy within 60s"
exit 1
REMOTE

echo "==> Deploy complete"
