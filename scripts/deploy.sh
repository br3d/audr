#!/usr/bin/env bash
# Deploy the application to the remote host via SSH.
# Pulls the latest images from the registry, runs migrations, restarts services.
# Usage: ./scripts/deploy.sh [TAG]
#   TAG — image tag to deploy (as produced by scripts/build.sh). Defaults to the
#         BACKEND_TAG/FRONTEND_TAG already pinned in the remote .env.
# Requires: SSH key at ./id_ed25519 (mode 600) or AUDR_SSH_KEY env var.
# Exits non-zero on any failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# scripts/ci.sh has always called `deploy.sh "${TAG}"`, but this script used to
# ignore positional arguments entirely — so the tag CI had just built was
# dropped and the deploy silently redeployed whatever BACKEND_TAG/FRONTEND_TAG
# the remote .env happened to pin. That is how AUD-360's first deploy pulled a
# stale image and advanced the database underneath it (2026-10-01).
DEPLOY_TAG="${1:-}"

DEPLOY_HOST="${DEPLOY_HOST:-codex@192.168.1.228}"
SSH_KEY="${AUDR_SSH_KEY:-${ROOT}/id_ed25519}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
# The live compose project runs out of /home/codex/audr — that is the directory
# in `com.docker.compose.project.working_dir` on the running containers, and the
# only one whose .env DB_PASSWORD matches the database role. /opt/audr and
# /srv/audr are stale copies left from earlier bring-ups; deploying into one of
# them pulls images and runs migrations against the live database using a stale
# password, which is how AUD-360's deploy first failed (2026-10-01).
REMOTE_DIR="${REMOTE_DIR:-/home/codex/audr}"

if [ -n "${DEPLOY_TAG}" ]; then
  echo "==> Deploying tag ${DEPLOY_TAG} to ${DEPLOY_HOST}:${REMOTE_DIR}"
else
  echo "==> Deploying to ${DEPLOY_HOST}:${REMOTE_DIR} (tag pinned in remote .env)"
fi

# Copy compose file to the remote host
scp -i "${SSH_KEY}" -o StrictHostKeyChecking=accept-new \
  "${ROOT}/compose.yaml" "${DEPLOY_HOST}:${REMOTE_DIR}/compose.yaml"

# Pull new images, run migrations, restart services
$SSH "${DEPLOY_HOST}" bash -s -- "${REMOTE_DIR}" "${DEPLOY_TAG}" <<'REMOTE'
set -euo pipefail
REMOTE_DIR="$1"
DEPLOY_TAG="${2:-}"
cd "${REMOTE_DIR}"

# Pin the requested tag before pulling so every later step — pull, migrate,
# up -d, and the image verification below — agrees on what is being deployed.
if [ -n "${DEPLOY_TAG}" ]; then
  echo "  -> Pinning BACKEND_TAG/FRONTEND_TAG to ${DEPLOY_TAG}"
  # Deliberately no .env backup here: only the two tag lines change, and .env
  # also holds DB_PASSWORD and SECRET_KEY — a copy per deploy would scatter the
  # secrets across the host for no recovery value.
  touch .env
  for key in BACKEND_TAG FRONTEND_TAG; do
    if grep -q "^${key}=" .env; then
      sed -i "s|^${key}=.*|${key}=${DEPLOY_TAG}|" .env
    else
      printf '%s=%s\n' "${key}" "${DEPLOY_TAG}" >> .env
    fi
  done
fi

echo "  -> Pulling latest images"
# `migrate` shares the backend image with api/worker, so pulling those covers it.
docker compose pull api web worker </dev/null

echo "  -> Running migrations"
# `-T` and `</dev/null` are both load-bearing. This whole script is fed to the
# remote shell on stdin (`bash -s` + heredoc), and `docker compose run` without
# `-T` attaches the container to stdin — so it swallowed the rest of this
# script. The deploy then ended right here, silently skipping the restart AND
# the readiness gate below, while still exiting 0 and printing "Deploy
# complete". Observed on 2026-10-01 (AUD-360): migrations advanced the database
# to a new head but api/web/worker kept running the previous image, leaving
# /health/ready at 503 behind a "successful" deploy.
docker compose run --rm -T migrate </dev/null

echo "  -> Restarting services"
docker compose up -d --remove-orphans </dev/null

# Fail loudly if a service is still on the old image: `up -d` is a no-op when it
# thinks nothing changed, and a deploy that silently keeps serving the previous
# build is the failure mode this gate exists to catch.
#
# Compare against the tags compose resolved rather than `docker compose config
# --images <svc>`: on compose 5.5.1 that subcommand ignores the service filter
# and prints every image in the project, so a per-service comparison against its
# first line matches the wrong image for `web`.
echo "  -> Verifying services are running the requested images"
#
# Read just the two tag keys out of .env rather than sourcing it: the file also
# holds DB_PASSWORD and SECRET_KEY, and sourcing would both pull secrets into
# this shell and break on any value containing shell metacharacters.
env_tag() {
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | tail -n1
}
backend_tag="${BACKEND_TAG:-$(env_tag BACKEND_TAG)}"
frontend_tag="${FRONTEND_TAG:-$(env_tag FRONTEND_TAG)}"
backend_tag="${backend_tag:-latest}"
frontend_tag="${frontend_tag:-latest}"
check_tag() {
  svc="$1"; want_tag="$2"
  cid="$(docker compose ps -q "${svc}" 2>/dev/null || true)"
  if [ -z "${cid}" ]; then
    echo "  -> ERROR: ${svc} has no running container after 'up -d'"
    exit 1
  fi
  got="$(docker inspect -f '{{.Config.Image}}' "${cid}" 2>/dev/null || true)"
  case "${got}" in
    *:"${want_tag}") : ;;
    *)
      echo "  -> ERROR: ${svc} runs '${got}', expected tag '${want_tag}'"
      exit 1
      ;;
  esac
}
check_tag api "${backend_tag}"
check_tag worker "${backend_tag}"
check_tag web "${frontend_tag}"

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
