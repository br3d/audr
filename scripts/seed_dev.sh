#!/usr/bin/env bash
# Seed a running audr instance with canonical dev/test fixtures (AUD-286).
#
# Creates the owner account with the test password, registers the Buterin
# wallet, and — when an RPC URL is available out of band — configures the RPC
# integration (AUD-349).  Safe to run multiple times — skips steps that are
# already done.
#
# Usage:
#   ./scripts/seed_dev.sh [BASE_URL]
#   BASE_URL defaults to http://localhost
#   (the app is served — SPA + /api + /health — by nginx on port 80)
#
# Demo wallet (AUDR_SEED_WALLET):
#   always (default) – register the Buterin wallet; already-present addresses
#                      are left untouched (the API answers 409)
#   skip             – never create it (use when you only want RPC configured)
#
# Seeding is deliberately unconditional (AUD-350): the point of the script is
# to fill a portfolio with enough real data to exercise every feature, so both
# the demo wallet and the RPC integration are always configured.
#
# The password seeded is the canonical test password defined in
# backend/tests/fixtures/seed.py (TEST_PASSWORD = "Rand0mP@ssw0rd").
#
# RPC URL (never committed — the URL embeds a provider API key):
#   resolved from the first source that is set, in this order
#     1. AUDR_SEED_RPC_URL environment variable
#     2. ./secrets/rpc_url.txt          (secrets/ is git-ignored)
#     3. AUDR_SEED_RPC_URL=... in ./.env (.env is git-ignored)
#   If none is present the RPC step is skipped with a note — seeding still
#   succeeds, and the URL can be entered later through Settings → Integrations.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="${1:-${AUDR_BASE_URL:-http://localhost}}"
API="${BASE_URL}/api/v1"
PASSWORD="Rand0mP@ssw0rd"
BUTERIN_ADDRESS="0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
BUTERIN_LABEL="Buterin"
COOKIE_JAR="$(mktemp)"
trap 'rm -f "${COOKIE_JAR}"' EXIT

_json_field() {
  # _json_field <field> <json_string>
  python3 -c "import sys,json; print(json.loads(sys.argv[2])[sys.argv[1]])" "$1" "$2"
}

_resolve_rpc_url() {
  # Echo the seed RPC URL from the first available out-of-git source, or
  # nothing when none is configured.  Never echo it to the console — callers
  # capture it into a variable and only ever print the hostname.
  if [[ -n "${AUDR_SEED_RPC_URL:-}" ]]; then
    printf '%s' "${AUDR_SEED_RPC_URL}"
    return
  fi
  if [[ -r "${ROOT}/secrets/rpc_url.txt" ]]; then
    tr -d '[:space:]' < "${ROOT}/secrets/rpc_url.txt"
    return
  fi
  if [[ -r "${ROOT}/.env" ]]; then
    # Last matching assignment wins; strip optional surrounding quotes.
    sed -n 's/^[[:space:]]*AUDR_SEED_RPC_URL=//p' "${ROOT}/.env" \
      | tail -n 1 \
      | tr -d '[:space:]' \
      | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
  fi
}

_url_host() {
  python3 -c "import sys,urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).hostname or '?')" "$1"
}

echo "==> Seeding ${BASE_URL}"

# ── 1. Setup / login ──────────────────────────────────────────────────────────

STATUS_JSON=$(curl -sf "${API}/setup/status")
SETUP_REQUIRED=$(_json_field setup_required "${STATUS_JSON}")

if [[ "${SETUP_REQUIRED}" == "True" ]]; then
  echo "  -> Setup required — creating owner with test password"
  SETUP_JSON=$(curl -sf -X POST "${API}/setup" \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"${PASSWORD}\"}" \
    -c "${COOKIE_JAR}")
  CSRF=$(_json_field csrf_token "${SETUP_JSON}")
  echo "  -> Owner created"
else
  echo "  -> Owner already exists — logging in with test password"
  LOGIN_JSON=$(curl -sf -X POST "${API}/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"${PASSWORD}\"}" \
    -c "${COOKIE_JAR}")
  CSRF=$(_json_field csrf_token "${LOGIN_JSON}")
  echo "  -> Logged in"
fi

# ── 2. Create Buterin wallet (idempotent: ignore 409 Conflict) ────────────────
#
# Unconditional by design (AUD-350): seeding exists to give a portfolio enough
# data to test with, so the demo wallet is always registered.  Re-runs are
# harmless — the address has a unique index and the API answers 409.  Set
# AUDR_SEED_WALLET=skip when you only want the RPC integration configured.

SEED_WALLET_MODE="${AUDR_SEED_WALLET:-always}"
WALLET_SEEDED="skipped"

if [[ "${SEED_WALLET_MODE}" == "skip" ]]; then
  echo "  -> AUDR_SEED_WALLET=skip — not creating the demo wallet"
else
  echo "  -> Registering wallet ${BUTERIN_ADDRESS} (${BUTERIN_LABEL})"
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${API}/wallets" \
    -H "Content-Type: application/json" \
    -H "x-csrf-token: ${CSRF}" \
    -b "${COOKIE_JAR}" \
    -d "{\"address\": \"${BUTERIN_ADDRESS}\", \"label\": \"${BUTERIN_LABEL}\"}")

  case "${HTTP_CODE}" in
    201) echo "  -> Wallet created (201)"; WALLET_SEEDED="${BUTERIN_ADDRESS} (${BUTERIN_LABEL})" ;;
    409) echo "  -> Wallet already registered (409), skipping"
         WALLET_SEEDED="${BUTERIN_ADDRESS} (already present)" ;;
    *)   echo "  -> ERROR: wallet creation returned HTTP ${HTTP_CODE}"; exit 1 ;;
  esac
fi

# ── 3. Configure the RPC integration (skipped when no URL is available) ──────

RPC_URL="$(_resolve_rpc_url)"
RPC_SEEDED="skipped"

if [[ -z "${RPC_URL}" ]]; then
  echo "  -> No RPC URL configured (AUDR_SEED_RPC_URL / secrets/rpc_url.txt / .env) — skipping RPC step"
else
  RPC_HOST="$(_url_host "${RPC_URL}")"
  echo "  -> Configuring RPC integration (host ${RPC_HOST})"

  INTEGRATIONS_JSON=$(curl -sf "${API}/integrations" -b "${COOKIE_JAR}")
  RPC_REVISION=$(python3 -c "
import sys, json
items = json.loads(sys.argv[1])['items']
print(next((i['revision'] for i in items if i['kind'] == 'rpc'), '0'))
" "${INTEGRATIONS_JSON}")

  RPC_BODY=$(python3 -c "
import json, sys
print(json.dumps({'revision': sys.argv[1], 'url': sys.argv[2]}))
" "${RPC_REVISION}" "${RPC_URL}")

  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "${API}/integrations/rpc" \
    -H "Content-Type: application/json" \
    -H "x-csrf-token: ${CSRF}" \
    -b "${COOKIE_JAR}" \
    -d "${RPC_BODY}")

  if [[ "${HTTP_CODE}" == "200" ]]; then
    echo "  -> RPC integration saved (revision ${RPC_REVISION} -> next)"
    RPC_SEEDED="${RPC_HOST}"
    # Kick off a validation run so the UI shows real health instead of
    # "unvalidated"; a failure here is not fatal for seeding.
    curl -sf -X POST "${API}/integrations/rpc/validate" \
      -H "x-csrf-token: ${CSRF}" \
      -b "${COOKIE_JAR}" \
      -o /dev/null && echo "  -> RPC validation job enqueued" \
      || echo "  -> WARNING: could not enqueue RPC validation job"
  else
    echo "  -> ERROR: RPC configuration returned HTTP ${HTTP_CODE}"
    exit 1
  fi
fi

# ── 4. Logout (clean up session) ─────────────────────────────────────────────

curl -sf -X POST "${API}/auth/logout" \
  -H "x-csrf-token: ${CSRF}" \
  -b "${COOKIE_JAR}" \
  -o /dev/null || true

echo ""
echo "==> Seed complete."
echo "    URL:      ${BASE_URL}"
echo "    Password: ${PASSWORD}"
echo "    Wallet:   ${WALLET_SEEDED}"
echo "    RPC:      ${RPC_SEEDED}"
