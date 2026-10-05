#!/usr/bin/env bash
# One-time credential initialisation for a fresh audr install.
# Run from the repo root: bash scripts/setup-secrets.sh
#
# Generates .env (DB_PASSWORD, SECRET_KEY) and keeps a recovery copy of the
# key in secrets/master_key.hex. Idempotent — existing files are never
# overwritten, so it is safe to re-run.
#
# Nothing else needs configuring here: blockchain RPC endpoints and price
# providers are set up in the web interface on the Connections page, and
# work without any key by default.
set -euo pipefail

SECRETS_DIR="$(dirname "$0")/../secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

if [ ! -f "$SECRETS_DIR/master_key.hex" ]; then
    # 32 random bytes encoded as 64 lowercase hex chars — used as SECRET_KEY env var.
    openssl rand -hex 32 | tr -d '\n' > "$SECRETS_DIR/master_key.hex"
    chmod 600 "$SECRETS_DIR/master_key.hex"
    echo "Generated secrets/master_key.hex"
else
    echo "secrets/master_key.hex already exists — keeping it"
fi

ENV_FILE="$(dirname "$0")/../.env"
if [ ! -f "$ENV_FILE" ]; then
    # The database password lives only in .env: the DATABASE_URL that the
    # api/worker/migrate services use embeds it, so a separate secrets file
    # would only be a second copy of the same value.
    #
    # Hex, not base64: the password is interpolated unencoded into the
    # postgresql+psycopg://audr:${DB_PASSWORD}@db URL in compose.yaml, and
    # base64's '/', '+' and '=' characters break that URL. 32 random bytes as
    # 64 lowercase hex chars is URL-safe and keeps the same entropy.
    if [ -f "$SECRETS_DIR/db_password.txt" ]; then
        # An older install that kept the password in its own file: the database
        # role was created with it, so reuse it — a freshly generated one would
        # not authenticate against the existing db_data volume.
        DB_PASSWORD="$(cat "$SECRETS_DIR/db_password.txt")"
        echo "Reusing the password from the pre-existing secrets/db_password.txt"
    else
        DB_PASSWORD="$(openssl rand -hex 32 | tr -d '\n')"
    fi
    SECRET_KEY="$(cat "$SECRETS_DIR/master_key.hex")"
    {
        printf 'DB_PASSWORD=%s\n' "$DB_PASSWORD"
        printf 'SECRET_KEY=%s\n' "$SECRET_KEY"
        printf '\n'
        printf '# Host port the web interface is published on (default 80).\n'
        printf '# Uncomment and change if something else already owns :80.\n'
        printf '# AUDR_HTTP_PORT=8080\n'
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Generated .env with DB_PASSWORD and SECRET_KEY"
else
    echo ".env already exists — keeping it"
fi

echo "Done. Credentials live in .env and secrets/ — both are git-ignored; back them up."
