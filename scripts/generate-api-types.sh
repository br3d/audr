#!/usr/bin/env bash
# Regenerate frontend/src/api/schema.gen.ts from the backend's OpenAPI schema (AUD-694).
#
# `export_openapi_json()` (backend/src/audr/operations/export_openapi.py) needs the
# audr package importable but no database and no SECRET_KEY/DATABASE_URL, so this runs
# it in the same backend-tests image compose.test.yaml already builds for pytest, with
# --no-deps — no need to bring up db-test/provider-mock just to read a schema.
# openapi-typescript then turns that JSON into frontend/src/api/schema.gen.ts, using the
# pinned toolchain in frontend/tools/openapi-codegen (see that directory's package.json
# for why it is not a frontend/ dependency: openapi-typescript's runtime needs
# typescript@5's `ts.factory`, which the app's own typescript@7 pin removed).
#
# Usage: ./scripts/generate-api-types.sh [--check]
#   --check   Generate into a temp file and diff against the committed schema.gen.ts,
#             without touching the working tree. Non-zero exit on drift — the CI gate.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_TEST="${ROOT}/compose.test.yaml"
# Unique project per invocation, same reasoning as scripts/test.sh: two concurrent
# runs on the same host must not share a compose project.
PROJECT="audr-types-$$"
COMPOSE=(docker compose -p "${PROJECT}" -f "${COMPOSE_TEST}")
SCHEMA_JSON="${ROOT}/backend/openapi.json"
GEN_TS="${ROOT}/frontend/src/api/schema.gen.ts"
CODEGEN_DIR="${ROOT}/frontend/tools/openapi-codegen"

CHECK=0
for arg in "$@"; do
  case "${arg}" in
    --check) CHECK=1 ;;
    *)
      echo "Unknown option: ${arg}" >&2
      echo "Usage: $0 [--check]" >&2
      exit 1
      ;;
  esac
done

TARGET="${GEN_TS}"
cleanup() {
  "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  [[ "${CHECK}" -eq 1 ]] && rm -f "${TARGET}"
  return 0
}
trap cleanup EXIT

echo "==> Building backend-tests image"
"${COMPOSE[@]}" build backend-tests

echo "==> Exporting backend OpenAPI schema"
# PYTHONHASHSEED=0: `/api/v1/auth/password` (backend/src/audr/api/auth.py) registers
# PUT and PATCH on one `api_route`/handler. FastAPI assigns that route a single
# operationId built from one method name picked out of `route.methods`, a `set` —
# so which of `..._put`/`..._patch` wins is hash-seed-dependent and flips across
# process runs with the interpreter's default randomized hashing. Unpinned, every
# regeneration had a ~50% chance of diffing against the committed file with zero
# real schema change, which makes a diff-based CI gate useless. Pinning makes this
# script's output reproducible; it does not fix the underlying ambiguity, which
# belongs to that route (an explicit `operation_id=` removes it outright) — tracked
# as a backend follow-up.
"${COMPOSE[@]}" run --rm --no-deps --no-TTY -e PYTHONHASHSEED=0 backend-tests \
  python -m audr.operations.export_openapi > "${SCHEMA_JSON}"

if [[ ! -d "${CODEGEN_DIR}/node_modules" ]]; then
  echo "==> Installing codegen toolchain"
  ( cd "${CODEGEN_DIR}" && npm ci --no-audit --no-fund )
fi

if [[ "${CHECK}" -eq 1 ]]; then
  TARGET="$(mktemp -t schema.gen.XXXXXX.ts)"
fi

echo "==> Generating TypeScript types"
"${CODEGEN_DIR}/node_modules/.bin/openapi-typescript" "${SCHEMA_JSON}" -o "${TARGET}"

if [[ "${CHECK}" -eq 1 ]]; then
  if ! diff -u "${GEN_TS}" "${TARGET}"; then
    echo "==> frontend/src/api/schema.gen.ts is stale relative to the backend schema." >&2
    echo "    Run: ./scripts/generate-api-types.sh" >&2
    exit 1
  fi
  echo "==> schema.gen.ts matches the backend schema"
fi
