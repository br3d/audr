#!/usr/bin/env bash
# Encrypted Postgres backup (AUD-390).
#
# Dumps the `db` service via `docker compose exec`, pipes the dump straight
# through an encryption tool (never touching disk unencrypted), and writes a
# timestamped archive to a configurable destination directory.
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
# secrets/master_key.hex (the KEK) is copied out SEPARATELY, in its own file,
# never bundled into the encrypted dump — see docs/operations.md#backups for
# why that separation matters.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS_DIR="${AUDR_SECRETS_DIR:-${ROOT}/secrets}"
BACKUP_KEY_FILE="${SECRETS_DIR}/backup_key.txt"
DEST_DIR="${1:-${AUDR_BACKUP_DIR:-${ROOT}/backups}}"
COMPOSE_PROJECT="${AUDR_COMPOSE_PROJECT:-audr}"
DB_SERVICE="${AUDR_DB_SERVICE:-db}"
DB_NAME="${AUDR_DB_NAME:-audr}"
DB_USER="${AUDR_DB_USER:-audr}"

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
  DUMP_PATH="${DEST_DIR}/audr-${TIMESTAMP}.sql.age"
else
  if [ ! -f "${BACKUP_KEY_FILE}" ]; then
    mkdir -p "${SECRETS_DIR}"
    chmod 700 "${SECRETS_DIR}"
    echo "No backup passphrase found — generating one at ${BACKUP_KEY_FILE} (gpg fallback, 'age' not installed)"
    openssl rand -base64 32 > "${BACKUP_KEY_FILE}"
    chmod 600 "${BACKUP_KEY_FILE}"
    echo "Keep ${BACKUP_KEY_FILE} safe and backed up elsewhere — losing it makes every past backup permanently unreadable."
  fi
  DUMP_PATH="${DEST_DIR}/audr-${TIMESTAMP}.sql.gpg"
fi

echo "Dumping ${DB_NAME} via 'docker compose exec' and encrypting with ${ENGINE}..."
if [ "${ENGINE}" = "age" ]; then
  docker compose -p "${COMPOSE_PROJECT}" exec -T "${DB_SERVICE}" \
    pg_dump -U "${DB_USER}" -d "${DB_NAME}" --format=plain \
    | age -r "${RECIPIENT}" -o "${DUMP_PATH}"
else
  docker compose -p "${COMPOSE_PROJECT}" exec -T "${DB_SERVICE}" \
    pg_dump -U "${DB_USER}" -d "${DB_NAME}" --format=plain \
    | gpg --symmetric --batch --yes --cipher-algo AES256 \
      --passphrase-file "${BACKUP_KEY_FILE}" -o "${DUMP_PATH}"
fi
chmod 600 "${DUMP_PATH}"
echo "Backup written: ${DUMP_PATH}"

MASTER_KEY_SRC="${SECRETS_DIR}/master_key.hex"
if [ -f "${MASTER_KEY_SRC}" ]; then
  MASTER_KEY_DEST="${DEST_DIR}/master_key-${TIMESTAMP}.hex"
  cp "${MASTER_KEY_SRC}" "${MASTER_KEY_DEST}"
  chmod 600 "${MASTER_KEY_DEST}"
  echo "Master key copied separately: ${MASTER_KEY_DEST}"
  echo "This file is NOT part of the encrypted dump. Store it with the same care as"
  echo "${MASTER_KEY_SRC} — without it, provider credentials inside the dump above are"
  echo "permanently unreadable, even after a successful restore."
else
  echo "WARNING: ${MASTER_KEY_SRC} not found — provider credentials cannot be recovered from this backup." >&2
fi
