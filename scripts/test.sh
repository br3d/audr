#!/usr/bin/env bash
# Run the full test suite: backend (pytest) and frontend (Vitest) in Docker Compose.
# Usage: ./scripts/test.sh
# Exits non-zero if any suite fails.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_TEST="${ROOT}/compose.test.yaml"
EXIT=0

echo "==> Starting test environment"
docker compose -f "${COMPOSE_TEST}" up -d db

echo "==> Waiting for database"
docker compose -f "${COMPOSE_TEST}" run --rm wait-for-db 2>/dev/null || \
  sleep 5  # fallback if wait-for-db service is not yet defined

echo "==> Running backend tests (pytest)"
docker compose -f "${COMPOSE_TEST}" run --rm backend-test \
  pytest --tb=short -q 2>&1 | tee "${ROOT}/.test-backend.log" || EXIT=$?

echo "==> Running frontend tests (Vitest)"
docker compose -f "${COMPOSE_TEST}" run --rm frontend-test \
  npx vitest run --reporter=verbose 2>&1 | tee "${ROOT}/.test-frontend.log" || EXIT=$?

echo "==> Tearing down test environment"
docker compose -f "${COMPOSE_TEST}" down -v --remove-orphans

if [[ "${EXIT}" -ne 0 ]]; then
  echo "==> TESTS FAILED (exit ${EXIT})"
else
  echo "==> All tests passed"
fi

exit "${EXIT}"
