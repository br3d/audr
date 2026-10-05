#!/usr/bin/env bash
# Run the full test suite: backend (pytest) and frontend (Vitest) in Docker Compose.
# Usage: ./scripts/test.sh [--backend-only|--frontend-only]
# Exits non-zero if any suite fails.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_TEST="${ROOT}/compose.test.yaml"
# Unique compose project per invocation. compose.test.yaml declares the fixed
# project name `audr-test`, so two concurrent runs on the same host — a CI job
# and a local run, say — would share containers, volumes and the test network,
# and either side's teardown `down -v` would destroy the other's database
# mid-run. The resulting red suites look like app regressions. Override with
# AUDR_TEST_PROJECT to deliberately reuse a stack.
PROJECT="${AUDR_TEST_PROJECT:-audr-test-$$}"
COMPOSE=(docker compose -p "${PROJECT}" -f "${COMPOSE_TEST}")
EXIT=0

RUN_BACKEND=1
RUN_FRONTEND=1
for arg in "$@"; do
  case "${arg}" in
    --backend-only)  RUN_FRONTEND=0 ;;
    --frontend-only) RUN_BACKEND=0  ;;
    *)
      echo "Unknown option: ${arg}"
      echo "Usage: $0 [--backend-only|--frontend-only]"
      exit 1
      ;;
  esac
done

teardown() {
  echo "==> Tearing down test environment"
  # `--rmi local` also removes the images Compose built for this project
  # (`<project>-migrate-test`, `<project>-backend-tests`). Without it every run
  # leaked two ~560MB images, because PROJECT is unique per invocation and so
  # nothing ever reclaimed the previous run's tags: 238 orphans had accumulated
  # on the agent host and took it to 97% disk. Rebuild cost is unaffected — a
  # per-run project name means the image was never reused anyway, and the shared
  # BuildKit cache still serves the layers. Skipped when the caller pinned
  # AUDR_TEST_PROJECT, since that opts into reusing a long-lived stack.
  local rmi=(--rmi local)
  [[ -n "${AUDR_TEST_PROJECT:-}" ]] && rmi=()
  "${COMPOSE[@]}" --profile backend --profile frontend down -v --remove-orphans "${rmi[@]}" || true
}
trap teardown EXIT

echo "==> Starting test environment (db-test, waiting for healthcheck)"
"${COMPOSE[@]}" up -d --wait db-test

if [[ "${RUN_BACKEND}" -eq 1 ]]; then
  # Rebuild both images: a stale migrate-test leaves the DB at an old migration
  # and backend tests then fail with "column does not exist".
  echo "==> Building backend test images (migrate-test, backend-tests)"
  "${COMPOSE[@]}" build migrate-test backend-tests

  echo "==> Running backend tests (pytest)"
  set +e
  "${COMPOSE[@]}" --profile backend run --rm --no-TTY backend-tests \
    2>&1 | tee "${ROOT}/.test-backend.log"
  BACKEND_EXIT="${PIPESTATUS[0]}"
  set -e
  echo "==> Backend suite exit=${BACKEND_EXIT}"
  if [[ "${BACKEND_EXIT}" -ne 0 ]]; then EXIT="${BACKEND_EXIT}"; fi
fi

if [[ "${RUN_FRONTEND}" -eq 1 ]]; then
  echo "==> Installing frontend dependencies"
  set +e
  "${COMPOSE[@]}" --profile frontend run --rm --no-TTY --entrypoint sh frontend-tests \
    -c 'npm ci --no-audit --no-fund' 2>&1 | tail -20
  FRONTEND_INSTALL_EXIT="${PIPESTATUS[0]}"
  set -e
  if [[ "${FRONTEND_INSTALL_EXIT}" -ne 0 ]]; then
    echo "==> Frontend dependency install FAILED (exit ${FRONTEND_INSTALL_EXIT})"
    EXIT="${FRONTEND_INSTALL_EXIT}"
  else
    echo "==> Running frontend tests (Vitest)"
    set +e
    "${COMPOSE[@]}" --profile frontend run --rm --no-TTY frontend-tests \
      2>&1 | tee "${ROOT}/.test-frontend.log"
    FRONTEND_EXIT="${PIPESTATUS[0]}"
    set -e
    echo "==> Frontend suite exit=${FRONTEND_EXIT}"
    if [[ "${FRONTEND_EXIT}" -ne 0 ]]; then EXIT="${FRONTEND_EXIT}"; fi
  fi
fi

if [[ "${EXIT}" -ne 0 ]]; then
  echo "==> TESTS FAILED (exit ${EXIT})"
else
  echo "==> All tests passed"
fi

exit "${EXIT}"
