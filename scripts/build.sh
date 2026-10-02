#!/usr/bin/env bash
# Build and push the application image to the private registry.
# Usage: ./scripts/build.sh [TAG]
#   TAG defaults to the short git SHA of HEAD.
# Exits non-zero on any failure.
#
# One image, not two. There used to be a second `audr-frontend` (nginx) image
# serving the SPA, but AUD-388 removed it: the `runtime` stage already contains
# the Vite build at /app/static and the API serves it directly. The frontend is
# still built here — it happens in the `frontend-builder` stage that `runtime`
# copies from.
set -euo pipefail

REGISTRY="${REGISTRY:-192.168.1.90:8085}"
TAG="${1:-$(git -C "$(dirname "$0")/.." rev-parse --short HEAD)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

BACKEND="${REGISTRY}/audr-backend"

echo "==> Building backend: ${BACKEND}:${TAG}"
docker build --platform linux/amd64 --target runtime \
  -t "${BACKEND}:${TAG}" -t "${BACKEND}:latest" "${ROOT}"

echo "==> Pushing backend"
docker push "${BACKEND}:${TAG}"
docker push "${BACKEND}:latest"

echo "==> Build complete: tag=${TAG}"
echo "${TAG}"
