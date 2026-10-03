#!/usr/bin/env bash
# Build and push the application image to the private registry.
# Usage: ./scripts/build.sh [TAG]
#   TAG defaults to the short git SHA of HEAD.
# Requires AUDR_REGISTRY (environment or deploy.env — see deploy.env.example).
# Exits non-zero on any failure.
#
# One image, not two. There used to be a second `audr-frontend` (nginx) image
# serving the SPA, but AUD-388 removed it: the `runtime` stage already contains
# the Vite build at /app/static and the API serves it directly. The frontend is
# still built here — it happens in the `frontend-builder` stage that `runtime`
# copies from.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/deploy-env.sh
. "${ROOT}/scripts/lib/deploy-env.sh"
audr_load_deploy_env "${ROOT}"

# REGISTRY stays supported as the older, script-local spelling; AUDR_REGISTRY is
# the one compose.yaml and deploy.env also use.
REGISTRY="${REGISTRY:-${AUDR_REGISTRY:-}}"
audr_require REGISTRY "The registry the deploy host pulls audr-backend from, e.g. registry.example.internal:5000."
TAG="${1:-$(git -C "${ROOT}" rev-parse --short HEAD)}"

BACKEND="${REGISTRY}/audr-backend"

echo "==> Building backend: ${BACKEND}:${TAG}"
docker build --platform linux/amd64 --target runtime \
  -t "${BACKEND}:${TAG}" -t "${BACKEND}:latest" "${ROOT}"

echo "==> Pushing backend"
docker push "${BACKEND}:${TAG}"
docker push "${BACKEND}:latest"

echo "==> Build complete: tag=${TAG}"
echo "${TAG}"
