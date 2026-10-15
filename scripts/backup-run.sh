#!/usr/bin/env bash
# Scheduled encrypted backup of the live stand (AUD-562).
#
# One run = take a dump, prove it decrypts, prune the directory to retention,
# and alert if any of that failed. Invoked by audr-backup.service; safe to run
# by hand on the deploy host for an off-schedule backup.
#
# WHY A WRAPPER rather than ExecStart=backup.sh. Three things have to happen
# together or the schedule is worse than useless:
#
#   1. backup.sh resolves its secrets, .env and destination relative to its own
#      location. Here the script is a pinned copy outside the compose dir, so
#      every path has to be passed in explicitly.
#   2. A dump nobody reads is not a backup. Each run decrypts its own output and
#      checks it starts like a pg_dump, which catches a truncated or
#      wrong-recipient archive on the day it happens instead of during a
#      restore we are already in trouble to be attempting.
#   3. A backup that silently stops is exactly the gap AUD-562 was filed for:
#      the tooling existed and was proven, and nothing ran it for two weeks. So
#      failure here alerts through scripts/notify.sh (board sink wakes an
#      assignee) rather than only landing in the journal.
#
# Environment:
#   AUDR_REMOTE_DIR   live compose project dir (default /home/codex/audr) —
#                     source of compose.yaml, .env and secrets/
#   AUDR_BACKUP_DIR   where dumps are written (default $AUDR_REMOTE_DIR/backups)
#   AUDR_BACKUP_DAYS  keep every dump from the last N days (default 7, matching
#                     audr-backup.service — a hand-run that defaulted tighter
#                     than the timer would silently delete days the timer kept)
#   AUDR_BACKUP_WEEKS keep at most N older weekly survivors (default 4). Both
#                     tiers are dump-prune.py's rule; the cap is what bounds the
#                     directory, measured on the stand at ~282 MiB per dump.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_DIR="${AUDR_REMOTE_DIR:-/home/codex/audr}"
BACKUP_DIR="${AUDR_BACKUP_DIR:-${REMOTE_DIR}/backups}"
DAYS="${AUDR_BACKUP_DAYS:-7}"
WEEKS="${AUDR_BACKUP_WEEKS:-4}"
STATE_FILE="${BACKUP_DIR}/.last-run-failed"

# backup.sh reads these instead of deriving them from its own directory, which
# here is the pinned install dir and holds neither secrets nor .env.
export AUDR_SECRETS_DIR="${REMOTE_DIR}/secrets"
export AUDR_ENV_FILE="${REMOTE_DIR}/.env"
# No KEK copy beside the dump — see the header of scripts/backup.sh.
export AUDR_BACKUP_SKIP_KEY_COPY=1
# notify.sh looks here for secrets/paperclip.env.
export AUDR_REMOTE_DIR="${REMOTE_DIR}"

log() { printf '[backup-run] %s\n' "$*"; }

fail() {
  log "FAILED: $*"
  # Already on a failure path: notify.sh exits 0 when unconfigured, and a
  # second failure from the alerting helper must not mask the first.
  printf 'audr stand backup FAILED on %s\n\n%s\n\nUnit: audr-backup.service. Check: journalctl -u audr-backup.service -n 50\nThe db_data volume is the only copy of the data while this is broken.\n' \
    "$(hostname)" "$*" \
    | "${HERE}/notify.sh" --key audr-backup-failed || true
  mkdir -p "${BACKUP_DIR}" 2>/dev/null
  : > "${STATE_FILE}" 2>/dev/null
  exit 1
}

cd "${REMOTE_DIR}" || fail "compose dir ${REMOTE_DIR} is not reachable"
[ -f compose.yaml ] || fail "${REMOTE_DIR}/compose.yaml not found — is this the live compose dir?"

log "taking dump into ${BACKUP_DIR}"
out="$("${HERE}/backup.sh" "${BACKUP_DIR}" 2>&1)" || fail "backup.sh exited non-zero:"$'\n'"${out}"
printf '%s\n' "${out}"

dump="$(printf '%s\n' "${out}" | sed -n 's/^Backup written: //p' | tail -1)"
if [ -z "${dump}" ] || [ ! -f "${dump}" ]; then
  fail "backup.sh reported no usable dump path:"$'\n'"${out}"
fi

size="$(stat -c %s "${dump}")"
# A valid dump of this schema is megabytes. Anything tiny is a failed pipe that
# still exited 0 somewhere upstream.
[ "${size}" -ge 1048576 ] || fail "dump ${dump} is only ${size} bytes — refusing to call that a backup"

# Decrypt to stdout and look at the header only; the plaintext never lands on
# disk, which is the whole point of the pipe in backup.sh.
log "verifying ${dump} decrypts (${size} bytes)"
case "${dump}" in
  *.age)
    head="$(age -d -i "${AUDR_SECRETS_DIR}/backup_key.txt" "${dump}" 2>/dev/null | head -c 4096)" ;;
  *.gpg)
    head="$(gpg --quiet --batch --yes --decrypt \
      --passphrase-file "${AUDR_SECRETS_DIR}/backup_key.txt" "${dump}" 2>/dev/null | head -c 4096)" ;;
  *) fail "unrecognised dump extension on ${dump}" ;;
esac
case "${head}" in
  *"PostgreSQL database dump"*) log "decrypt OK — header looks like pg_dump output" ;;
  "") fail "decrypting ${dump} produced nothing — the archive or the key is bad" ;;
  *)  fail "decrypted ${dump} does not look like a pg_dump" ;;
esac

log "pruning to ${DAYS} daily + at most ${WEEKS} older weekly survivors"
prune="$(/usr/bin/env python3 "${HERE}/dump-prune.py" \
  --dir "${BACKUP_DIR}" --prefix audr --days "${DAYS}" --weeks "${WEEKS}" --apply 2>&1)" \
  || fail "dump-prune.py exited non-zero:"$'\n'"${prune}"
printf '%s\n' "${prune}"

if [ -f "${STATE_FILE}" ]; then
  log "previous run had failed — standing the alert down"
  printf 'audr stand backup is working again on %s: %s (%s bytes), decrypt verified.\n' \
    "$(hostname)" "${dump}" "${size}" \
    | "${HERE}/notify.sh" --key audr-backup-failed --resolve || true
  rm -f "${STATE_FILE}"
fi

log "done"
