#!/usr/bin/env bash
# Restore an encrypted Postgres backup produced by scripts/backup.sh (AUD-390).
#
# Usage: ./scripts/restore.sh [--force] [--identity FILE] DUMP_FILE
#   DUMP_FILE    path to an audr-*.sql[.gz].age or audr-*.sql[.gz].gpg archive.
#   --identity   age identity / gpg passphrase file (default: secrets/backup_key.txt).
#   --force      required to restore over a database that already has tables.
#
# Both shapes are read, and that is load-bearing rather than politeness:
# backup.sh gzips before encrypting as of AUD-564, and the dumps written before
# that change are still the only copy of the days they cover. The `.gz` in the
# name is what selects the decompression step, so a dump taken either way
# restores with the same command.
#
# This restores the *data* only. If SECRET_KEY / secrets/master_key.hex does
# not match what the dump's `key_state` row was wrapped with, provider
# credentials in the restored database stay unreadable — restore the matching
# master_key.hex (backed up separately by backup.sh) alongside this dump.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS_DIR="${AUDR_SECRETS_DIR:-${ROOT}/secrets}"
IDENTITY_FILE="${SECRETS_DIR}/backup_key.txt"
COMPOSE_PROJECT="${AUDR_COMPOSE_PROJECT:-audr}"
DB_SERVICE="${AUDR_DB_SERVICE:-db}"
DB_NAME="${AUDR_DB_NAME:-audr}"
DB_USER="${AUDR_DB_USER:-audr}"
FORCE=0
DUMP_FILE=""

usage() {
  echo "Usage: $0 [--force] [--identity FILE] DUMP_FILE" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --force)
      FORCE=1
      shift
      ;;
    --identity)
      [ $# -ge 2 ] || usage
      IDENTITY_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      [ -z "${DUMP_FILE}" ] || usage
      DUMP_FILE="$1"
      shift
      ;;
  esac
done

[ -n "${DUMP_FILE}" ] || usage
[ -f "${DUMP_FILE}" ] || { echo "Dump file not found: ${DUMP_FILE}" >&2; exit 1; }

case "${DUMP_FILE}" in
  *.sql.age|*.sql.gz.age)
    command -v age >/dev/null 2>&1 || { echo "'age' not found on PATH" >&2; exit 1; }
    [ -f "${IDENTITY_FILE}" ] || { echo "age identity file not found: ${IDENTITY_FILE}" >&2; exit 1; }
    DECRYPT=(age -d -i "${IDENTITY_FILE}")
    ;;
  *.sql.gpg|*.sql.gz.gpg)
    command -v gpg >/dev/null 2>&1 || { echo "'gpg' not found on PATH" >&2; exit 1; }
    [ -f "${IDENTITY_FILE}" ] || { echo "gpg passphrase file not found: ${IDENTITY_FILE}" >&2; exit 1; }
    DECRYPT=(gpg --decrypt --batch --yes --passphrase-file "${IDENTITY_FILE}")
    ;;
  *)
    echo "Unrecognised dump extension (expected .sql[.gz].age or .sql[.gz].gpg): ${DUMP_FILE}" >&2
    exit 1
    ;;
esac

case "${DUMP_FILE}" in
  *.sql.gz.age|*.sql.gz.gpg)
    command -v gzip >/dev/null 2>&1 || { echo "'gzip' not found on PATH — needed for a .gz dump" >&2; exit 1; }
    DECOMPRESS=(gzip -dc)
    ;;
  *)
    DECOMPRESS=(cat)
    ;;
esac

TABLE_COUNT="$(docker compose -p "${COMPOSE_PROJECT}" exec -T "${DB_SERVICE}" \
  psql -U "${DB_USER}" -d "${DB_NAME}" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" | tr -d '[:space:]')"

if [ "${TABLE_COUNT}" != "0" ] && [ "${FORCE}" -ne 1 ]; then
  echo "Target database '${DB_NAME}' already has ${TABLE_COUNT} table(s) in the public schema." >&2
  echo "Refusing to restore over a non-empty database. Re-run with --force to drop and overwrite it." >&2
  exit 1
fi

if [ "${TABLE_COUNT}" != "0" ] && [ "${FORCE}" -eq 1 ]; then
  echo "--force set: dropping and recreating the public schema in '${DB_NAME}' before restore."
  docker compose -p "${COMPOSE_PROJECT}" exec -T "${DB_SERVICE}" \
    psql -U "${DB_USER}" -d "${DB_NAME}" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
fi

echo "Decrypting ${DUMP_FILE} and restoring into '${DB_NAME}'..."
"${DECRYPT[@]}" < "${DUMP_FILE}" \
  | "${DECOMPRESS[@]}" \
  | docker compose -p "${COMPOSE_PROJECT}" exec -T "${DB_SERVICE}" \
    psql -U "${DB_USER}" -d "${DB_NAME}"

echo "Restore complete. Remember to also restore the matching secrets/master_key.hex"
echo "(backed up separately as master_key-*.hex) if this host does not already have it."
