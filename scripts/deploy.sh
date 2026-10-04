#!/usr/bin/env bash
# Deploy the application to the remote host via SSH.
# Pulls the latest image from the registry, runs migrations, restarts services.
# Usage: ./scripts/deploy.sh [TAG]
#   TAG — image tag to deploy (as produced by scripts/build.sh). Defaults to the
#         BACKEND_TAG already pinned in the remote .env.
# Requires: SSH key at ./id_ed25519 (mode 600) or AUDR_SSH_KEY env var, plus
#   AUDR_DEPLOY_HOST and AUDR_REGISTRY from the environment or deploy.env
#   (see deploy.env.example). None of them is baked into this public repo.
# Exits non-zero on any failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/deploy-env.sh
. "${ROOT}/scripts/lib/deploy-env.sh"
audr_load_deploy_env "${ROOT}"

# scripts/ci.sh has always called `deploy.sh "${TAG}"`, but this script used to
# ignore positional arguments entirely — so the tag CI had just built was
# dropped and the deploy silently redeployed whatever BACKEND_TAG the remote
# .env happened to pin. That is how AUD-360's first deploy pulled a stale image
# and advanced the database underneath it (2026-10-01).
DEPLOY_TAG="${1:-}"

DEPLOY_HOST="${DEPLOY_HOST:-${AUDR_DEPLOY_HOST:-}}"
audr_require DEPLOY_HOST "SSH destination of the deploy host, e.g. deploy@audr.example.internal."
REGISTRY="${REGISTRY:-${AUDR_REGISTRY:-}}"
audr_require REGISTRY "Registry compose.yaml resolves \${AUDR_REGISTRY} against, e.g. registry.example.internal:5000."
SSH_KEY="${AUDR_SSH_KEY:-${ROOT}/id_ed25519}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
# The live compose project runs out of /home/codex/audr — that is the directory
# in `com.docker.compose.project.working_dir` on the running containers, and the
# only one whose .env DB_PASSWORD matches the database role. /opt/audr and
# /srv/audr are stale copies left from earlier bring-ups; deploying into one of
# them pulls images and runs migrations against the live database using a stale
# password, which is how AUD-360's deploy first failed (2026-10-01).
REMOTE_DIR="${REMOTE_DIR:-${AUDR_REMOTE_DIR:-/home/codex/audr}}"

if [ -n "${DEPLOY_TAG}" ]; then
  echo "==> Deploying tag ${DEPLOY_TAG} to ${DEPLOY_HOST}:${REMOTE_DIR}"
else
  echo "==> Deploying to ${DEPLOY_HOST}:${REMOTE_DIR} (tag pinned in remote .env)"
fi

# Copy compose file to the remote host
scp -i "${SSH_KEY}" -o StrictHostKeyChecking=accept-new \
  "${ROOT}/compose.yaml" "${DEPLOY_HOST}:${REMOTE_DIR}/compose.yaml"

# Pull new images, run migrations, restart services
$SSH "${DEPLOY_HOST}" bash -s -- "${REMOTE_DIR}" "${DEPLOY_TAG}" "${REGISTRY}" <<'REMOTE'
set -euo pipefail
REMOTE_DIR="$1"
DEPLOY_TAG="${2:-}"
REGISTRY="${3:?registry not passed from the local side}"
cd "${REMOTE_DIR}"

# Write a single KEY=value into the remote .env, replacing any existing line.
#
# Deliberately no .env backup here: only these keys change, and .env also holds
# DB_PASSWORD and SECRET_KEY — a copy per deploy would scatter the secrets
# across the host for no recovery value.
touch .env
env_set() {
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

# compose.yaml defaults to the public ghcr.io/br3d image (AUD-418); this host
# pulls from its own registry instead, so pin ${AUDR_REGISTRY} on every deploy —
# a host whose .env predates that variable, or a fresh bring-up, would otherwise
# pull the public release rather than the image this deploy just built.
env_set AUDR_REGISTRY "${REGISTRY}"

# Pin the requested tag before pulling so every later step — pull, migrate,
# up -d, and the image verification below — agrees on what is being deployed.
if [ -n "${DEPLOY_TAG}" ]; then
  echo "  -> Pinning BACKEND_TAG to ${DEPLOY_TAG}"
  # Only BACKEND_TAG since AUD-388: there is one image now. A stale FRONTEND_TAG
  # line may still sit in the remote .env from before that change; it is inert,
  # because no service in compose.yaml interpolates it any more.
  env_set BACKEND_TAG "${DEPLOY_TAG}"
fi

echo "  -> Pulling latest image"
# All three services share the backend image, so this one pull covers `migrate`
# too.
docker compose pull api worker </dev/null

echo "  -> Running migrations"
# `-T` and `</dev/null` are both load-bearing. This whole script is fed to the
# remote shell on stdin (`bash -s` + heredoc), and `docker compose run` without
# `-T` attaches the container to stdin — so it swallowed the rest of this
# script. The deploy then ended right here, silently skipping the restart AND
# the readiness gate below, while still exiting 0 and printing "Deploy
# complete". Observed on 2026-10-01 (AUD-360): migrations advanced the database
# to a new head but api/worker kept running the previous image, leaving
# /health/ready at 503 behind a "successful" deploy.
docker compose run --rm -T migrate </dev/null

echo "  -> Restarting services"
docker compose up -d --remove-orphans </dev/null

# Fail loudly if a service is still on the old image: `up -d` is a no-op when it
# thinks nothing changed, and a deploy that silently keeps serving the previous
# build is the failure mode this gate exists to catch.
#
# Compare against the tag compose resolved rather than `docker compose config
# --images <svc>`: on compose 5.5.1 that subcommand ignores the service filter
# and prints every image in the project, so a per-service comparison against its
# first line can match the wrong image.
echo "  -> Verifying services are running the requested image"
#
# Read just the tag key out of .env rather than sourcing it: the file also holds
# DB_PASSWORD and SECRET_KEY, and sourcing would both pull secrets into this
# shell and break on any value containing shell metacharacters.
env_tag() {
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | tail -n1
}
backend_tag="${BACKEND_TAG:-$(env_tag BACKEND_TAG)}"
backend_tag="${backend_tag:-latest}"
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

echo "  -> Waiting for API readiness gate"
# Gate on /health/ready, NOT /health (AUD-328).
#
# `/health/ready` performs a real DB + master-key check inside FastAPI, so it
# cannot be satisfied by anything but a working API. A bare `/health` is not a
# route at all, and since AUD-388 the API also serves the SPA from a catch-all
# mount — so the thing to be careful about is a gate URL that the SPA fallback
# could answer with index.html and HTTP 200 while the app is actually broken.
# `audr.api.spa` reserves `/health` and `/api` against that fallback, so both
# 404 honestly rather than returning the shell.
#
# Belt and braces regardless: we additionally require the body to be the API's
# JSON (`"status":"ok"`), so even if the SPA mount ever did answer here, an
# index.html response fails the gate instead of silently passing it.
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
