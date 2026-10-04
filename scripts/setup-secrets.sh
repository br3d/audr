#!/usr/bin/env bash
# One-time secrets initialisation for a fresh deployment host.
# Run from the repo root: bash scripts/setup-secrets.sh
set -euo pipefail

SECRETS_DIR="$(dirname "$0")/../secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

if [ ! -f "$SECRETS_DIR/master_key.hex" ]; then
    # 32 random bytes encoded as 64 lowercase hex chars — used as SECRET_KEY env var.
    openssl rand -hex 32 | tr -d '\n' > "$SECRETS_DIR/master_key.hex"
    chmod 600 "$SECRETS_DIR/master_key.hex"
    echo "Generated master_key.hex"
else
    echo "master_key.hex already exists — skipping"
fi

# Ethereum RPC URL used by scripts/seed_dev.sh (AUD-349).  The URL embeds a
# provider API key, so it lives only here — never in git.  Supply it once via
# AUDR_SEED_RPC_URL; ask infraLead for the shared value if you do not have it.
if [ ! -f "$SECRETS_DIR/rpc_url.txt" ]; then
    if [ -n "${AUDR_SEED_RPC_URL:-}" ]; then
        printf '%s' "$AUDR_SEED_RPC_URL" > "$SECRETS_DIR/rpc_url.txt"
        chmod 600 "$SECRETS_DIR/rpc_url.txt"
        echo "Wrote rpc_url.txt from AUDR_SEED_RPC_URL"
    else
        echo "rpc_url.txt not set — re-run with AUDR_SEED_RPC_URL=... to seed the RPC integration"
    fi
else
    echo "rpc_url.txt already exists — skipping"
fi

ENV_FILE="$(dirname "$0")/../.env"
if [ ! -f "$ENV_FILE" ]; then
    # The database password lives only in .env (AUD-418). It used to be written
    # to secrets/db_password.txt as well and mounted into the db container as a
    # Docker secret, but compose needs the same value in .env anyway — the
    # DATABASE_URL the api/worker/migrate services use embeds it — so the file
    # was a second copy of it with nothing to gain.
    #
    # Hex, not base64: the password is interpolated unencoded into the
    # postgresql+psycopg://audr:${DB_PASSWORD}@db URL in compose.yaml, and
    # base64's '/', '+' and '=' characters break that URL. 32 random bytes as
    # 64 lowercase hex chars is URL-safe and keeps the same entropy.
    if [ -f "$SECRETS_DIR/db_password.txt" ]; then
        # A host installed before AUD-418: the database role was created with
        # this password, so reuse it — a fresh one would not authenticate
        # against the existing db_data volume.
        DB_PASSWORD="$(cat "$SECRETS_DIR/db_password.txt")"
        echo "Reusing the password from the pre-existing secrets/db_password.txt"
    else
        DB_PASSWORD="$(openssl rand -hex 32 | tr -d '\n')"
    fi
    SECRET_KEY="$(cat "$SECRETS_DIR/master_key.hex")"
    {
        printf 'DB_PASSWORD=%s\n' "$DB_PASSWORD"
        printf 'SECRET_KEY=%s\n' "$SECRET_KEY"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Generated .env with DB_PASSWORD and SECRET_KEY"
else
    echo ".env already exists — skipping"
fi

echo "Credentials are in $ENV_FILE and $SECRETS_DIR — keep both outside version control."
