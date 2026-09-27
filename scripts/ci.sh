#!/usr/bin/env bash
# Full CI pipeline: build → test → deploy → smoke-test.
# Usage: ./scripts/ci.sh [--skip-tests] [--skip-deploy] [--skip-smoke]
# Exits non-zero on any stage failure (unless that stage is skipped).
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"

SKIP_TESTS=0
SKIP_DEPLOY=0
SKIP_SMOKE=0

for arg in "$@"; do
  case "${arg}" in
    --skip-tests)  SKIP_TESTS=1  ;;
    --skip-deploy) SKIP_DEPLOY=1 ;;
    --skip-smoke)  SKIP_SMOKE=1  ;;
    *)
      echo "Unknown option: ${arg}"
      echo "Usage: $0 [--skip-tests] [--skip-deploy] [--skip-smoke]"
      exit 1
      ;;
  esac
done

echo "========================================"
echo " AUDR CI pipeline"
echo "========================================"

# ── Stage 1: Build ────────────────────────────────────────────────────────────
echo ""
echo "── Stage 1/4: Build ─────────────────────"
TAG=$("${SCRIPTS}/build.sh" | tail -1)
echo "Built tag: ${TAG}"

# ── Stage 2: Tests ───────────────────────────────────────────────────────────
echo ""
echo "── Stage 2/4: Tests ─────────────────────"
if [[ "${SKIP_TESTS}" -eq 1 ]]; then
  echo "  (skipped)"
else
  "${SCRIPTS}/test.sh"
fi

# ── Stage 3: Deploy ──────────────────────────────────────────────────────────
echo ""
echo "── Stage 3/4: Deploy ────────────────────"
if [[ "${SKIP_DEPLOY}" -eq 1 ]]; then
  echo "  (skipped)"
else
  "${SCRIPTS}/deploy.sh" "${TAG}"
fi

# ── Stage 4: Smoke test ──────────────────────────────────────────────────────
echo ""
echo "── Stage 4/4: Smoke test ────────────────"
if [[ "${SKIP_SMOKE}" -eq 1 ]]; then
  echo "  (skipped)"
else
  "${SCRIPTS}/smoke-test.sh"
fi

echo ""
echo "========================================"
echo " Pipeline complete  tag=${TAG}"
echo "========================================"
