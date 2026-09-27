#!/usr/bin/env bash
# One-time secrets initialisation for a fresh deployment host.
# Run from the repo root: bash scripts/setup-secrets.sh
set -euo pipefail

SECRETS_DIR="$(dirname "$0")/../secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

if [ ! -f "$SECRETS_DIR/db_password.txt" ]; then
    openssl rand -base64 32 > "$SECRETS_DIR/db_password.txt"
    chmod 600 "$SECRETS_DIR/db_password.txt"
    echo "Generated db_password.txt"
else
    echo "db_password.txt already exists — skipping"
fi

if [ ! -f "$SECRETS_DIR/master_key.bin" ]; then
    openssl rand -out "$SECRETS_DIR/master_key.bin" 32
    chmod 600 "$SECRETS_DIR/master_key.bin"
    echo "Generated master_key.bin"
else
    echo "master_key.bin already exists — skipping"
fi

echo "Secrets are in $SECRETS_DIR — keep this directory outside version control."
