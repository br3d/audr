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

echo "  -> Waiting for API readiness gate"
# Gate on /health/ready, NOT /health (AUD-328).
#
# nginx proxies `^/(api|health)/` — note the trailing slash — so a bare
# `/health` never reaches the API: it falls through to the SPA `try_files`
# fallback and returns index.html with HTTP 200 even when the api container is
# dead.  `/health/ready` matches the proxy regex AND performs a real DB +
# master-key check inside FastAPI, so it cannot be satisfied by the SPA.
#
# Belt and braces: we additionally require the body to be the API's JSON
# (`"status":"ok"`), so if the nginx location regex is ever loosened, an
# index.html response still fails the gate instead of silently passing it.
HEALTH_URL="http://localhost/health/ready"
for i in $(seq 1 30); do
  body="$(curl -s -m 5 -o - -w '\n%{http_code}' "${HEALTH_URL}" 2>/dev/null || true)"
  code="$(printf '%s' "${body}" | tail -n1)"
  if [ "${code}" = "200" ] && printf '%s' "${body}" | grep -q '"status":"ok"'; then
    echo "  -> API is ready (attempt ${i})"
    exit 0
  fi
  sleep 2
done
echo "  -> ERROR: API did not become ready within 60s (last status=${code:-000})"
echo "  -> Last response from ${HEALTH_URL}:"
printf '%s\n' "${body:-<none>}" | head -5
docker compose ps || true
exit 1
REMOTE

echo "==> Deploy complete"
