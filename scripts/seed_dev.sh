#!/usr/bin/env bash
# Seed a running audr instance with canonical dev/test fixtures (AUD-286).
#
# Creates the owner account with the test password and registers the Buterin
# wallet.  Safe to run multiple times — skips steps that are already done.
#
# Usage:
#   ./scripts/seed_dev.sh [BASE_URL]
#   BASE_URL defaults to http://localhost:8080
#
# The password seeded is the canonical test password defined in
# backend/tests/fixtures/seed.py (TEST_PASSWORD = "Rand0mP@ssw0rd").
set -euo pipefail

BASE_URL="${1:-${AUDR_BASE_URL:-http://localhost:8080}}"
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

echo "  -> Registering wallet ${BUTERIN_ADDRESS} (${BUTERIN_LABEL})"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${API}/wallets" \
  -H "Content-Type: application/json" \
  -H "x-csrf-token: ${CSRF}" \
  -b "${COOKIE_JAR}" \
  -d "{\"address\": \"${BUTERIN_ADDRESS}\", \"label\": \"${BUTERIN_LABEL}\"}")

case "${HTTP_CODE}" in
  201) echo "  -> Wallet created (201)" ;;
  409) echo "  -> Wallet already registered (409), skipping" ;;
  *)   echo "  -> ERROR: wallet creation returned HTTP ${HTTP_CODE}"; exit 1 ;;
esac

# ── 3. Logout (clean up session) ─────────────────────────────────────────────

curl -sf -X POST "${API}/auth/logout" \
  -H "x-csrf-token: ${CSRF}" \
  -b "${COOKIE_JAR}" \
  -o /dev/null || true

echo ""
echo "==> Seed complete."
echo "    URL:      ${BASE_URL}"
echo "    Password: ${PASSWORD}"
echo "    Wallet:   ${BUTERIN_ADDRESS} (${BUTERIN_LABEL})"
