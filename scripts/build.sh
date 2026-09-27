#!/usr/bin/env bash
# Build and push the application Docker image to the private registry.
# Usage: ./scripts/build.sh [TAG]
#   TAG defaults to the short git SHA of HEAD.
# Exits non-zero on any failure.
set -euo pipefail

REGISTRY="${REGISTRY:-192.168.1.90:8085}"
IMAGE="${IMAGE:-audr/app}"
TAG="${1:-$(git -C "$(dirname "$0")/.." rev-parse --short HEAD)}"
FULL="${REGISTRY}/${IMAGE}:${TAG}"
LATEST="${REGISTRY}/${IMAGE}:latest"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Building image: ${FULL}"
docker build --platform linux/amd64 -t "${FULL}" -t "${LATEST}" "${ROOT}"

echo "==> Pushing ${FULL}"
docker push "${FULL}"

echo "==> Pushing ${LATEST}"
docker push "${LATEST}"

echo "==> Build complete: ${FULL}"
echo "${TAG}"        # last line is the tag — callers can capture it
