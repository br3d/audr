#!/usr/bin/env bash
# Encrypted Postgres backup (AUD-390).
#
# Dumps the `db` service via `docker compose exec`, pipes the dump through gzip
# and then straight through an encryption tool (never touching disk
# unencrypted), and writes a timestamped archive to a configurable destination
# directory.
#
# COMPRESS BEFORE ENCRYPTING, not after (AUD-564). Ciphertext is
# incompressible, so the order is not a preference. Measured on the stand's
# live database: `age` on a plain pg_dump produced 295,260,620 bytes (282 MiB);
# gzipped first, the same database produced 51,621,096 bytes (49 MiB) — 5.7x.
# The archive is `audr-<ts>.sql.gz.age`; restore.sh reads that shape and the
# older uncompressed `.sql.age` / `.sql.gpg` ones.
#
# AUDR_BACKUP_COMPRESS=none writes the old uncompressed shape, for an operator
# who wants a dump readable with `age -d` alone and no gzip on the far end.
#
# Usage: ./scripts/backup.sh [DEST_DIR]
#   DEST_DIR defaults to ./backups (override with AUDR_BACKUP_DIR or $1).
#
# Recipient key: `age` is preferred (single static binary, no passphrase to
# manage). If secrets/backup_key.txt does not exist yet, one is generated on
# first run — this is the same zero-config-first philosophy as
# scripts/setup-secrets.sh, so the very first backup never blocks on key
# ceremony. Set AUDR_BACKUP_RECIPIENT to an existing age public key
# (age1...) to use a recipient whose private key you hold elsewhere instead.
#
# Falls back to `gpg --symmetric` (AES256) only when `age` is not installed;
# see docs/operations.md#backups for the tradeoffs.
#
# The KEK is copied out SEPARATELY, in its own file, never bundled into the
# encrypted dump — see docs/operations.md#backups for why that separation
# matters. It is read from secrets/master_key.hex, or, on a host that only has
# the rendered env file (the deploy hosts — setup-secrets.sh puts the same value
# in .env as SECRET_KEY and the deploy path ships no secrets/master_key.hex),
# from SECRET_KEY in .env.
#
# AUDR_BACKUP_SKIP_KEY_COPY=1 suppresses that copy. The scheduled on-host
# backup (AUD-562) sets it: a KEK file beside the dump it decrypts gives an
# attacker who can read the host both halves at once, which is the duplicate
# AUD-426 removed from this very directory. An operator taking a dump to carry
# off the host wants the copy and so leaves this unset.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS_DIR="${AUDR_SECRETS_DIR:-${ROOT}/secrets}"
BACKUP_KEY_FILE="${SECRETS_DIR}/backup_key.txt"
DEST_DIR="${1:-${AUDR_BACKUP_DIR:-${ROOT}/backups}}"
COMPOSE_PROJECT="${AUDR_COMPOSE_PROJECT:-audr}"
DB_SERVICE="${AUDR_DB_SERVICE:-db}"
DB_NAME="${AUDR_DB_NAME:-audr}"
DB_USER="${AUDR_DB_USER:-audr}"
COMPRESS="${AUDR_BACKUP_COMPRESS:-gzip}"

case "${COMPRESS}" in
  gzip)
    command -v gzip >/dev/null 2>&1 || {
      echo "'gzip' is not on PATH — set AUDR_BACKUP_COMPRESS=none to take an uncompressed dump." >&2
      exit 1
    }
    SQL_EXT=".sql.gz"
    ;;
  none)
    SQL_EXT=".sql"
    ;;
  *)
    echo "AUDR_BACKUP_COMPRESS must be 'gzip' or 'none', got '${COMPRESS}'" >&2
    exit 1
    ;;
esac

mkdir -p "${DEST_DIR}"
chmod 700 "${DEST_DIR}"

TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"

if command -v age >/dev/null 2>&1; then
  ENGINE="age"
elif command -v gpg >/dev/null 2>&1; then
  ENGINE="gpg"
else
  echo "Neither 'age' nor 'gpg' is on PATH — install one before taking a backup." >&2
  exit 1
fi

RECIPIENT=""
if [ "${ENGINE}" = "age" ]; then
  if [ -n "${AUDR_BACKUP_RECIPIENT:-}" ]; then
    RECIPIENT="${AUDR_BACKUP_RECIPIENT}"
  else
    if [ ! -f "${BACKUP_KEY_FILE}" ]; then
      command -v age-keygen >/dev/null 2>&1 || {
        echo "'age' found but 'age-keygen' is missing — cannot generate a recipient key automatically." >&2
        echo "Either install age-keygen, or set AUDR_BACKUP_RECIPIENT to an existing age public key." >&2
        exit 1
      }
      mkdir -p "${SECRETS_DIR}"
      chmod 700 "${SECRETS_DIR}"
      echo "No backup key found — generating one at ${BACKUP_KEY_FILE}"
      age-keygen -o "${BACKUP_KEY_FILE}" 2>&1
      chmod 600 "${BACKUP_KEY_FILE}"
      echo "Keep ${BACKUP_KEY_FILE} safe and backed up elsewhere — losing it makes every past backup permanently unreadable."
    fi
    RECIPIENT="$(grep -m1 '^# public key:' "${BACKUP_KEY_FILE}" | sed 's/^# public key: *//')"
    [ -n "${RECIPIENT}" ] || {
      echo "Could not read a public key out of ${BACKUP_KEY_FILE}" >&2
      exit 1
    }
  fi
  echo "Backup recipient: ${RECIPIENT}"
  DUMP_PATH="${DEST_DIR}/audr-${TIMESTAMP}${SQL_EXT}.age"
else
  if [ ! -f "${BACKUP_KEY_FILE}" ]; then
    mkdir -p "${SECRETS_DIR}"
    chmod 700 "${SECRETS_DIR}"
    echo "No backup passphrase found — generating one at ${BACKUP_KEY_FILE} (gpg fallback, 'age' not installed)"
    openssl rand -base64 32 > "${BACKUP_KEY_FILE}"
    chmod 600 "${BACKUP_KEY_FILE}"
    echo "Keep ${BACKUP_KEY_FILE} safe and backed up elsewhere — losing it makes every past backup permanently unreadable."
  fi
  DUMP_PATH="${DEST_DIR}/audr-${TIMESTAMP}${SQL_EXT}.gpg"
fi

# `cat` rather than a conditional pipeline: one code path writes the dump, so
# the compressed and uncompressed shapes cannot drift in how they are produced.
if [ "${COMPRESS}" = "gzip" ]; then
  COMPRESSOR=(gzip -6)
else
  COMPRESSOR=(cat)
fi

if [ "${ENGINE}" = "age" ]; then
  ENCRYPTOR=(age -r "${RECIPIENT}" -o "${DUMP_PATH}")
else
  ENCRYPTOR=(gpg --symmetric --batch --yes --cipher-algo AES256
    --passphrase-file "${BACKUP_KEY_FILE}" -o "${DUMP_PATH}")
fi

if [ "${COMPRESS}" = "gzip" ]; then
  echo "Dumping ${DB_NAME} via 'docker compose exec', compressing with gzip and encrypting with ${ENGINE}..."
else
  echo "Dumping ${DB_NAME} via 'docker compose exec' and encrypting with ${ENGINE} (uncompressed)..."
fi
# pipefail is on, so a pg_dump or gzip failure mid-stream fails the whole run
# rather than leaving a short, perfectly decryptable, truncated archive.
docker compose -p "${COMPOSE_PROJECT}" exec -T "${DB_SERVICE}" \
  pg_dump -U "${DB_USER}" -d "${DB_NAME}" --format=plain \
  | "${COMPRESSOR[@]}" \
  | "${ENCRYPTOR[@]}"
chmod 600 "${DUMP_PATH}"
echo "Backup written: ${DUMP_PATH}"

# Where the KEK lives on this host. setup-secrets.sh writes both, but the
# deploy path renders only .env — so a missing master_key.hex is normal there
# and must not be reported as "the key is gone".
MASTER_KEY_SRC="${SECRETS_DIR}/master_key.hex"
ENV_FILE="${AUDR_ENV_FILE:-${ROOT}/.env}"
KEK_SOURCE=""
if [ -f "${MASTER_KEY_SRC}" ]; then
  KEK_SOURCE="${MASTER_KEY_SRC}"
elif [ -f "${ENV_FILE}" ] && grep -q '^SECRET_KEY=' "${ENV_FILE}"; then
  KEK_SOURCE="${ENV_FILE} (SECRET_KEY)"
fi

if [ -z "${KEK_SOURCE}" ]; then
  echo "WARNING: no KEK found (looked in ${MASTER_KEY_SRC} and ${ENV_FILE}) —" >&2
  echo "provider credentials and wallet addresses cannot be recovered from this backup." >&2
elif [ "${AUDR_BACKUP_SKIP_KEY_COPY:-}" = "1" ]; then
  echo "KEK not copied (AUDR_BACKUP_SKIP_KEY_COPY=1); it stays only in ${KEK_SOURCE}."
  echo "This dump alone does NOT survive the loss of this host — carry the dump and"
  echo "that key off-host together to cover that case. See docs/operations.md#backups."
else
  MASTER_KEY_DEST="${DEST_DIR}/master_key-${TIMESTAMP}.hex"
  if [ -f "${MASTER_KEY_SRC}" ]; then
    cp "${MASTER_KEY_SRC}" "${MASTER_KEY_DEST}"
  else
    # Same 64 hex chars, taken from the one place this host keeps them. No
    # trailing newline, matching what setup-secrets.sh writes.
    ( umask 077; printf '%s' "$(grep -m1 '^SECRET_KEY=' "${ENV_FILE}" | cut -d= -f2-)" > "${MASTER_KEY_DEST}" )
  fi
  chmod 600 "${MASTER_KEY_DEST}"
  echo "Master key copied separately from ${KEK_SOURCE}: ${MASTER_KEY_DEST}"
  echo "This file is NOT part of the encrypted dump. Store it with the same care as"
  echo "the source — without it, provider credentials and wallet addresses inside the"
  echo "dump above are permanently unreadable, even after a successful restore."
fi
