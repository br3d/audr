#!/usr/bin/env bash
# One-time secrets initialisation for a fresh deployment host.
# Run from the repo root: bash scripts/setup-secrets.sh
set -euo pipefail

SECRETS_DIR="$(dirname "$0")/../secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

if [ ! -f "$SECRETS_DIR/db_password.txt" ]; then
    # Hex, not base64: the password is interpolated unencoded into the
    # postgresql+psycopg://audr:${DB_PASSWORD}@db URL in compose.yaml, and
    # base64's '/', '+' and '=' characters break that URL. 32 random bytes as
    # 64 lowercase hex chars is URL-safe and keeps the same entropy.
    # Existing hosts: rotating an already-working base64 password is optional —
    # this only affects fresh installs.
    openssl rand -hex 32 | tr -d '\n' > "$SECRETS_DIR/db_password.txt"
    chmod 600 "$SECRETS_DIR/db_password.txt"
    echo "Generated db_password.txt"
else
    echo "db_password.txt already exists — skipping"
fi

if [ ! -f "$SECRETS_DIR/master_key.hex" ]; then
    # 32 random bytes encoded as 64 lowercase hex chars — used as SECRET_KEY env var.
    openssl rand -hex 32 | tr -d '\n' > "$SECRETS_DIR/master_key.hex"
    chmod 600 "$SECRETS_DIR/master_key.hex"
    echo "Generated master_key.hex"
else
    echo "master_key.hex already exists — skipping"
fi

ENV_FILE="$(dirname "$0")/../.env"
if [ ! -f "$ENV_FILE" ]; then
    DB_PASSWORD="$(cat "$SECRETS_DIR/db_password.txt")"
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

echo "Secrets are in $SECRETS_DIR — keep this directory outside version control."
