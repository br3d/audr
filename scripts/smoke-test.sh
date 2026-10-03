#!/usr/bin/env bash
# Run a quick smoke test against the live deployment.
# Usage: ./scripts/smoke-test.sh [BASE_URL]
#   BASE_URL defaults to $AUDR_BASE_URL, else http://localhost (the api container
#   publishes 80, so the default is right when run on the deploy host itself;
#   set AUDR_BASE_URL in deploy.env to probe it from elsewhere)
# Exits non-zero if any check fails.
#
# Assertions are written against ROUTES THAT ACTUALLY EXIST (AUD-328).  Two
# traps to be aware of when editing this file:
#
#  1. SPA fallback.  Since AUD-388 FastAPI serves the SPA itself from a
#     catch-all mount, so index.html comes back with HTTP 200 for any path that
#     matches no API route and is not under the reserved `/api` or `/health`
#     prefixes.  A 404 assertion on a made-up *frontend* path (e.g.
#     /nonexistent-route-xyz) therefore can never pass.  Any check meant to
#     prove the BACKEND is alive must target a path under /api/ or /health/ AND
#     assert on the response body, not on the status code alone.
#  2. `curl -f` suppresses the real status code on HTTP errors, which is why
#     this script does not use it — we want to read 401/404 back verbatim.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/deploy-env.sh
. "${ROOT}/scripts/lib/deploy-env.sh"
audr_load_deploy_env "${ROOT}"

BASE_URL="${1:-${AUDR_BASE_URL:-http://localhost}}"
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

# Backend liveness — body proves it is not the SPA fallback.
check "API liveness  /health/live"        "${BASE_URL}/health/live"  200 '"status":"ok"'
# Backend readiness — real DB + master-key check inside FastAPI.
check "API readiness /health/ready"       "${BASE_URL}/health/ready" 200 '"status":"ok"'
# Authenticated route rejects anonymous callers (proves API routing + auth work).
check "Auth session rejects anonymous"    "${BASE_URL}/api/v1/auth/session" 401
# Unknown API path must 404 from FastAPI. NOTE: an unknown *frontend* path
# returns 200 (index.html) by design — only /api/ paths can assert 404.
check "Unknown API path 404s"             "${BASE_URL}/api/v1/nonexistent" 404
# Frontend SPA is served — by the api container itself since AUD-388.
check "Frontend SPA loads"                "${BASE_URL}/" 200 '<div id="root"'
# Hard refresh on a deep client-side route must serve the shell, not a 404.
# This is the `try_files ... /index.html` behaviour the removed nginx container
# used to provide, and the single most likely thing to regress if the static
# mount in audr.api.spa is ever reordered or replaced.
check "SPA deep-route refresh /folio"     "${BASE_URL}/folio" 200 '<div id="root"'
# AUD-407: the deployment reports a semantic version, and it is the one this
# checkout carries. Asserting the exact number — not merely that the route
# answers — is what turns the smoke test into a deploy check: a stale image that
# is otherwise perfectly healthy fails here and nowhere else.
#
# Set AUDR_EXPECT_VERSION to pin a different number, e.g. when smoke-testing a
# rollback, where the live build is deliberately older than this working copy.
EXPECT_VERSION="${AUDR_EXPECT_VERSION:-$(tr -d ' \t\r\n' < "${ROOT}/VERSION")}"
check "Deployed version is ${EXPECT_VERSION}" "${BASE_URL}/api/v1/version" 200 "\"version\":\"${EXPECT_VERSION}\""

echo ""
echo "==> Results: ${PASS} passed, ${FAIL} failed"

if [[ "${FAIL}" -gt 0 ]]; then
  exit 1
fi
