#!/usr/bin/env bash
# Build and push the application image to the private registry.
# Usage: ./scripts/build.sh [TAG]
#   TAG defaults to the semantic image tag for the current tree,
#   `<version>-g<short sha>` (AUD-407) — see scripts/lib/version.sh for why the
#   sha stays in the tag. Building from a release tag additionally pushes the
#   clean `<version>` tag.
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
# shellcheck source=scripts/lib/version.sh
. "${ROOT}/scripts/lib/version.sh"
audr_load_deploy_env "${ROOT}"

# REGISTRY stays supported as the older, script-local spelling; AUDR_REGISTRY is
# the one compose.yaml and deploy.env also use.
REGISTRY="${REGISTRY:-${AUDR_REGISTRY:-}}"
audr_require REGISTRY "The registry the deploy host pulls audr-backend from, e.g. registry.example.internal:5000."
VERSION="$(audr_version "${ROOT}")"
GIT_SHA="$(git -C "${ROOT}" rev-parse HEAD)"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TAG="${1:-$(audr_image_tag "${ROOT}" "$(git -C "${ROOT}" rev-parse --short=12 HEAD)")}"

BACKEND="${REGISTRY}/audr-backend"

# `latest` is always moved; the bare `<version>` tag is only published when HEAD
# is exactly the `v<version>` release tag. Publishing it from an arbitrary commit
# would make `:1.4.2` mean "whatever was built last" instead of "the 1.4.2
# release", which is the one guarantee semver tagging is supposed to buy.
EXTRA_TAGS=("latest")
if [ "$(git -C "${ROOT}" rev-parse -q --verify "refs/tags/v${VERSION}^{commit}" 2>/dev/null)" = "${GIT_SHA}" ]; then
  EXTRA_TAGS+=("${VERSION}")
  echo "==> HEAD is release v${VERSION} — will also publish :${VERSION}"
fi

echo "==> Building backend: ${BACKEND}:${TAG} (version ${VERSION})"
TAG_ARGS=(-t "${BACKEND}:${TAG}")
for t in "${EXTRA_TAGS[@]}"; do TAG_ARGS+=(-t "${BACKEND}:${t}"); done
docker build --platform linux/amd64 --target runtime \
  --build-arg "AUDR_VERSION=${VERSION}" \
  --build-arg "AUDR_GIT_SHA=${GIT_SHA}" \
  --build-arg "AUDR_BUILT_AT=${BUILT_AT}" \
  "${TAG_ARGS[@]}" "${ROOT}"

echo "==> Pushing backend"
docker push "${BACKEND}:${TAG}"
for t in "${EXTRA_TAGS[@]}"; do docker push "${BACKEND}:${t}"; done

echo "==> Build complete: tag=${TAG}"
echo "${TAG}"
