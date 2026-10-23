#!/usr/bin/env bash
# Regenerate frontend/src/api/schema.gen.ts from the backend's committed
# OpenAPI schema (AUD-694).
#
# Freshness of backend/openapi.json itself (does it match the live FastAPI
# app?) is NOT this script's job — that's
# backend/tests/unit/test_export_openapi.py::test_committed_schema_matches_the_live_app,
# which runs as part of the ordinary pytest suite in both CI systems. This
# script only needs Node: it runs openapi-typescript (pinned to
# typescript@5 in this directory's own node_modules, since its runtime needs
# `ts.factory`, which the frontend app's typescript@7 pin removed) against
# that already-checked-out file.
#
# Usage: ./generate.sh [--export] [--check]
#   --export  Re-export backend/openapi.json from the live app first, via a
#             one-off container built from the backend test image.
#             export_openapi_json() opens no DB connection (see
#             backend/src/audr/operations/export_openapi.py), so this skips
#             compose.test.yaml's db-test/provider-mock dependency chain —
#             no need to boot the full test stack just to read route shapes.
#             Requires Docker; use this after a backend schema change.
#   --check   Regenerate into a temp file and diff against the committed
#             schema.gen.ts instead of overwriting it. Exits non-zero on
#             drift — the CI gate (AUD-694). Needs only Node.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
SCHEMA="${ROOT}/backend/openapi.json"
COMMITTED="${ROOT}/frontend/src/api/schema.gen.ts"

CHECK=0
EXPORT=0
for arg in "$@"; do
  case "${arg}" in
    --check) CHECK=1 ;;
    --export) EXPORT=1 ;;
    *)
      echo "Usage: $0 [--export] [--check]" >&2
      exit 2
      ;;
  esac
done

if [[ "${EXPORT}" -eq 1 ]]; then
  echo "==> Building backend test image"
  docker build -q -f "${ROOT}/Dockerfile.test" -t audr-openapi-codegen "${ROOT}" >/dev/null

  echo "==> Exporting backend OpenAPI schema"
  docker run --rm -v "${ROOT}/backend:/workspace/backend:ro" audr-openapi-codegen \
    python -m audr.operations.export_openapi > "${SCHEMA}"
fi

echo "==> Installing openapi-typescript (isolated toolchain)"
( cd "${HERE}" && npm ci --no-audit --no-fund >/dev/null )

if [[ "${CHECK}" -eq 1 ]]; then
  GENERATED="$(mktemp)"
  trap 'rm -f "${GENERATED}"' EXIT
  ( cd "${HERE}" && npx openapi-typescript "${SCHEMA}" -o "${GENERATED}" )
  echo "==> Diffing against committed schema.gen.ts"
  diff -u "${COMMITTED}" "${GENERATED}"
else
  echo "==> Generating TypeScript types"
  ( cd "${HERE}" && npx openapi-typescript "${SCHEMA}" -o "${COMMITTED}" )
fi
