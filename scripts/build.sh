#!/usr/bin/env bash
# Build and push backend and frontend images to the private registry.
# Usage: ./scripts/build.sh [TAG]
#   TAG defaults to the short git SHA of HEAD.
# Exits non-zero on any failure.
set -euo pipefail

REGISTRY="${REGISTRY:-192.168.1.90:8085}"
TAG="${1:-$(git -C "$(dirname "$0")/.." rev-parse --short HEAD)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

BACKEND="${REGISTRY}/audr-backend"
FRONTEND="${REGISTRY}/audr-frontend"

echo "==> Building backend: ${BACKEND}:${TAG}"
docker build --platform linux/amd64 --target runtime \
  -t "${BACKEND}:${TAG}" -t "${BACKEND}:latest" "${ROOT}"

echo "==> Building frontend: ${FRONTEND}:${TAG}"
docker build --platform linux/amd64 --target frontend-server \
  -t "${FRONTEND}:${TAG}" -t "${FRONTEND}:latest" "${ROOT}"

echo "==> Pushing backend"
docker push "${BACKEND}:${TAG}"
docker push "${BACKEND}:latest"

echo "==> Pushing frontend"
docker push "${FRONTEND}:${TAG}"
docker push "${FRONTEND}:latest"

echo "==> Build complete: tag=${TAG}"
echo "${TAG}"
