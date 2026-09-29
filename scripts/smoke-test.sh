#!/usr/bin/env bash
# Run a quick smoke test against the live deployment.
# Usage: ./scripts/smoke-test.sh [BASE_URL]
#   BASE_URL defaults to http://192.168.1.228 (nginx serves on port 80)
# Exits non-zero if any check fails.
#
# Assertions are written against ROUTES THAT ACTUALLY EXIST (AUD-328).  Two
# traps to be aware of when editing this file:
#
#  1. SPA fallback.  nginx proxies only `^/(api|health)/` (trailing slash) and
#     serves index.html with HTTP 200 for everything else.  So a 404 assertion
#     on a made-up *frontend* path (e.g. /nonexistent-route-xyz) can never pass,
#     and a 200 assertion on a bare /health passes even with a dead API.  Any
#     check that is meant to prove the BACKEND is alive must therefore target a
#     path under /api/ or /health/ AND assert on the response body/content-type,
#     not on the status code alone.
#  2. `curl -f` suppresses the real status code on HTTP errors, which is why
#     this script does not use it — we want to read 401/404 back verbatim.
set -euo pipefail

BASE_URL="${1:-${AUDR_BASE_URL:-http://192.168.1.228}}"
PASS=0
FAIL=0

# check <label> <url> [expected_status] [expected_body_substring]
check() {
  local label="$1"
  local url="$2"
  local expected_status="${3:-200}"
  local expected_body="${4:-}"
  local response status body
  # No -f: we need the true status code for 401/404 assertions.
  response="$(curl -s -m 10 -o - -w $'\n%{http_code}' "${url}" 2>/dev/null || true)"
  status="$(printf '%s' "${response}" | tail -n1)"
  body="$(printf '%s' "${response}" | sed '$d')"
  [ -n "${status}" ] || status="000"

  if [[ "${status}" != "${expected_status}" ]]; then
    echo "  FAIL  ${label} — expected HTTP ${expected_status}, got ${status} (${url})"
    FAIL=$((FAIL + 1))
    return
  fi
  if [[ -n "${expected_body}" ]] && ! printf '%s' "${body}" | grep -qF "${expected_body}"; then
    echo "  FAIL  ${label} — HTTP ${status} ok but body missing '${expected_body}' (${url})"
    echo "        got: $(printf '%s' "${body}" | head -c 120)"
    FAIL=$((FAIL + 1))
    return
  fi
  echo "  PASS  ${label} (${status})"
  PASS=$((PASS + 1))
}

echo "==> Smoke test: ${BASE_URL}"

# Backend liveness — proxied to FastAPI; body proves it is not the SPA fallback.
check "API liveness  /health/live"        "${BASE_URL}/health/live"  200 '"status":"ok"'
# Backend readiness — real DB + master-key check inside FastAPI.
check "API readiness /health/ready"       "${BASE_URL}/health/ready" 200 '"status":"ok"'
# Authenticated route rejects anonymous callers (proves API routing + auth work).
check "Auth session rejects anonymous"    "${BASE_URL}/api/v1/auth/session" 401
# Unknown API path must 404 from FastAPI. NOTE: an unknown *frontend* path
# returns 200 (index.html) by design — only /api/ paths can assert 404.
check "Unknown API path 404s"             "${BASE_URL}/api/v1/nonexistent" 404
# Frontend SPA is served.
check "Frontend SPA loads"                "${BASE_URL}/" 200 '<div id="root"'

echo ""
echo "==> Results: ${PASS} passed, ${FAIL} failed"

if [[ "${FAIL}" -gt 0 ]]; then
  exit 1
fi
