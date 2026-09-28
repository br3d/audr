#!/usr/bin/env bash
# Run a quick smoke test against the live deployment.
# Usage: ./scripts/smoke-test.sh [BASE_URL]
#   BASE_URL defaults to http://192.168.1.228 (nginx serves on port 80)
# Exits non-zero if any check fails.
set -euo pipefail

BASE_URL="${1:-${AUDR_BASE_URL:-http://192.168.1.228}}"
PASS=0
FAIL=0

check() {
  local label="$1"
  local url="$2"
  local expected_status="${3:-200}"
  local status
  status=$(curl -sf -o /dev/null -w "%{http_code}" "${url}" 2>/dev/null || echo "000")
  if [[ "${status}" == "${expected_status}" ]]; then
    echo "  PASS  ${label} (${status})"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  ${label} — expected ${expected_status}, got ${status} (${url})"
    FAIL=$((FAIL + 1))
  fi
}

echo "==> Smoke test: ${BASE_URL}"

check "Health endpoint"            "${BASE_URL}/health"
check "API root responds"          "${BASE_URL}/api/v1/health"
check "Frontend SPA loads"         "${BASE_URL}/"
check "404 returns proper status"  "${BASE_URL}/nonexistent-route-xyz" "404"

echo ""
echo "==> Results: ${PASS} passed, ${FAIL} failed"

if [[ "${FAIL}" -gt 0 ]]; then
  exit 1
fi
