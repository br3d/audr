#!/usr/bin/env bash
# Deploy the application to the remote host via SSH.
# Usage: ./scripts/deploy.sh [TAG]
#   TAG defaults to "latest". Pass the output of build.sh for a pinned deploy.
# Requires: SSH key at ./id_ed25519 (mode 600) or AUDR_SSH_KEY env var.
# Exits non-zero on any failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

REGISTRY="${REGISTRY:-192.168.1.90:8085}"
IMAGE="${IMAGE:-audr/app}"
TAG="${1:-latest}"
FULL="${REGISTRY}/${IMAGE}:${TAG}"

DEPLOY_HOST="${DEPLOY_HOST:-codex@192.168.1.228}"
SSH_KEY="${AUDR_SSH_KEY:-${ROOT}/id_ed25519}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
REMOTE_DIR="${REMOTE_DIR:-/opt/audr}"

echo "==> Deploying ${FULL} to ${DEPLOY_HOST}:${REMOTE_DIR}"

# Copy compose file to the remote host
scp -i "${SSH_KEY}" -o StrictHostKeyChecking=accept-new \
  "${ROOT}/compose.yaml" "${DEPLOY_HOST}:${REMOTE_DIR}/compose.yaml"

# Pull, migrate, and restart on the remote host
$SSH "${DEPLOY_HOST}" bash -s -- "${FULL}" "${TAG}" "${REGISTRY}/${IMAGE}:latest" <<'REMOTE'
set -euo pipefail
FULL="$1"
TAG="$2"
LATEST="$3"
REMOTE_DIR="${REMOTE_DIR:-/opt/audr}"
cd "${REMOTE_DIR}"

echo "  -> Pulling ${FULL}"
docker pull "${FULL}"
docker tag "${FULL}" "${LATEST}"

echo "  -> Running migrations"
AUDR_IMAGE="${FULL}" docker compose run --rm migrate

echo "  -> Restarting services"
AUDR_IMAGE="${FULL}" docker compose up -d --remove-orphans api worker

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

echo "==> Deploy complete: ${FULL}"
