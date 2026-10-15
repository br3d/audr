#!/usr/bin/env bash
# Install (or re-install) the scheduled encrypted database backup on the deploy
# host (AUD-562).
#
#   scripts/install-backup.sh            install/update, take a first dump, show status
#   scripts/install-backup.sh --check    report what is installed; no changes
#
# What it puts on the host:
#   <install dir>/scripts/{backup-run.sh,backup.sh,dump-prune.py,restore.sh,
#                          notify.sh,notify-board.py}   verbatim repo copies
#
# restore.sh is shipped even though no timer calls it: the moment it is needed
# is a lost db_data volume, and an operator in that position should find the
# restore tool sitting next to the dumps rather than have to clone the repo
# onto a host that is already having a bad day.
#   /etc/systemd/system/audr-backup.service             from the .in template
#   /etc/systemd/system/audr-backup.timer
#
# Why pinned copies rather than pointing the unit at a git checkout: the same
# reason as scripts/install-watchdog.sh. The only checkout on that host belongs
# to the CI runner, which rewrites it on every deploy and resets it on a failed
# one — so a backup whose code vanishes during a bad deploy would stop at
# exactly the moment the next deploy is most likely to need it.
#
# Re-running is the update path and is idempotent. Changing backup.sh,
# backup-run.sh or dump-prune.py in this repo does NOT reach the host until
# this runs.
#
# SCOPE, stated here because it is easy to over-read a green timer: this writes
# one encrypted dump per day to the same host the database runs on. It covers
# volume loss, a bad migration and an accidental delete. It does NOT cover loss
# of the host, and no KEK is written beside the dumps — see
# docs/operations.md#backups.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/deploy-env.sh
. "$REPO_DIR/scripts/lib/deploy-env.sh"
audr_load_deploy_env "$REPO_DIR"

HOST="${AUDR_DEPLOY_HOST:-}"
audr_require AUDR_DEPLOY_HOST "SSH destination of the deploy host that should back itself up."
KEY="${AUDR_SSH_KEY:-$REPO_DIR/id_ed25519}"
REMOTE_DIR="${AUDR_REMOTE_DIR:-/home/codex/audr}"
INSTALL_DIR="${AUDR_BACKUP_INSTALL_DIR:-.local/share/audr-backup}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$HOST")

mode=apply
case "${1:-}" in
  --check) mode=check ;;
  "")      ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

if [ "$mode" = check ]; then
  "${SSH[@]}" "
    echo '== units =='
    systemctl list-unit-files audr-backup.\\* --no-pager 2>/dev/null | head -5 || true
    echo '== timer =='
    systemctl list-timers audr-backup.timer --no-pager 2>/dev/null | head -3 || true
    echo '== last run =='
    systemctl status audr-backup.service --no-pager -n 10 2>/dev/null | tail -14 || echo 'not installed'
    echo '== dumps =='
    # The directory is 700 and the dumps 600, so this only reads as the owner.
    if [ -d '$REMOTE_DIR/backups' ]; then
      ls -la '$REMOTE_DIR/backups' | tail -n +2
      du -sh '$REMOTE_DIR/backups'
      df -h --output=avail / | tail -1 | sed 's/^/  root fs available: /'
    else
      echo 'NO BACKUPS DIRECTORY — nothing has ever been dumped on this host'
    fi
    echo '== last run outcome =='
    if [ -f '$REMOTE_DIR/backups/.last-run-failed' ]; then
      echo 'THE LAST RUN FAILED and has not since succeeded (see the alert it raised)'
    else
      echo 'no failure flag'
    fi
    echo '== encryption =='
    if command -v age >/dev/null 2>&1; then
      echo \"age present (\$(age --version 2>&1 | head -1)) — dumps are .sql.age\"
    elif command -v gpg >/dev/null 2>&1; then
      echo 'age ABSENT, gpg present — dumps take the .sql.gpg fallback path'
    else
      echo 'NEITHER age NOR gpg — backup.sh cannot encrypt and will refuse to run'
    fi
    for f in '$REMOTE_DIR/secrets/backup_key.txt'; do
      if [ -f \"\$f\" ]; then echo \"\$f present (\$(stat -c %a \"\$f\"))\"; else echo \"\$f absent — generated on the first run\"; fi
    done
    echo '== KEK =='
    # Not copied beside the dumps by design; it still has to EXIST, or the
    # dumps restore without wallet addresses or provider credentials.
    if [ -f '$REMOTE_DIR/secrets/master_key.hex' ]; then
      echo 'secrets/master_key.hex present'
    elif grep -q '^SECRET_KEY=' '$REMOTE_DIR/.env' 2>/dev/null; then
      echo '.env SECRET_KEY present — this is the KEK on a deploy-style host'
    else
      echo 'NO KEK FOUND — a restore from these dumps cannot decrypt wallet addresses'
    fi
  "
  exit 0
fi

# Absolute install path, resolved on the host so ~ belongs to the remote user.
remote_home="$("${SSH[@]}" 'echo "$HOME"')"
abs_install="$remote_home/${INSTALL_DIR#"$remote_home/"}"

echo "==> installing backup scripts to $HOST:$abs_install/scripts"
"${SSH[@]}" "mkdir -p '$abs_install/scripts'"
# notify.sh resolves its sibling notify-board.py relative to its own directory,
# so both travel together or alerting silently degrades to a log line.
scp -q -i "$KEY" -o StrictHostKeyChecking=no \
  "$REPO_DIR/scripts/backup-run.sh" "$REPO_DIR/scripts/backup.sh" \
  "$REPO_DIR/scripts/dump-prune.py" "$REPO_DIR/scripts/restore.sh" \
  "$REPO_DIR/scripts/notify.sh" "$REPO_DIR/scripts/notify-board.py" \
  "$HOST:$abs_install/scripts/"
"${SSH[@]}" "chmod 755 '$abs_install/scripts/backup-run.sh' '$abs_install/scripts/backup.sh' \
  '$abs_install/scripts/dump-prune.py' '$abs_install/scripts/restore.sh' \
  '$abs_install/scripts/notify.sh' '$abs_install/scripts/notify-board.py'"

echo "==> checking the host can encrypt"
if ! "${SSH[@]}" 'command -v age >/dev/null 2>&1'; then
  if "${SSH[@]}" 'command -v gpg >/dev/null 2>&1'; then
    echo "    NOTE: 'age' is not installed; dumps will take the gpg --symmetric"
    echo "    fallback (.sql.gpg). Both are AES-256 and restore.sh reads either."
    echo "    'age' is preferred and is in the Ubuntu archive: sudo apt install age"
  else
    echo "ERROR: the host has neither 'age' nor 'gpg' — backup.sh cannot encrypt." >&2
    echo "       Install one (sudo apt install age) and re-run." >&2
    exit 1
  fi
fi

echo "==> rendering and installing systemd units (sudo on the host)"
service="$(sed -e "s|@INSTALL_DIR@|$abs_install|g" -e "s|@REMOTE_DIR@|$REMOTE_DIR|g" \
  "$REPO_DIR/ci/host-units/audr-backup.service.in")"

# Units go in via stdin rather than scp-to-/etc: the SSH user has no write
# access there, and this keeps the privileged step to one explicit sudo tee.
printf '%s\n' "$service" | "${SSH[@]}" 'sudo tee /etc/systemd/system/audr-backup.service >/dev/null'
"${SSH[@]}" 'sudo tee /etc/systemd/system/audr-backup.timer >/dev/null' \
  < "$REPO_DIR/ci/host-units/audr-backup.timer"

"${SSH[@]}" 'sudo systemctl daemon-reload && sudo systemctl enable --now audr-backup.timer'

# A timer that is enabled but has never fired proves nothing. This runs the real
# unit once, synchronously, so the install either produces a verified dump or
# fails here where someone is watching.
echo "==> first backup (runs the unit, not a copy of it)"
"${SSH[@]}" 'sudo systemctl start audr-backup.service; systemctl status audr-backup.service --no-pager -n 40 | tail -32'

echo
echo "==> installed. Timer:"
"${SSH[@]}" 'systemctl list-timers audr-backup.timer --no-pager | head -3'
echo
echo "==> dumps on the host:"
"${SSH[@]}" "ls -la '$REMOTE_DIR/backups' 2>/dev/null | tail -n +2; du -sh '$REMOTE_DIR/backups' 2>/dev/null"
cat <<EOF

==> WHAT THIS DOES AND DOES NOT PROTECT

    Covered:     loss of the db_data volume, a bad migration, an accidental
                 \`docker compose down -v\`, a table dropped by hand.
    NOT covered: loss of this host. The dumps live on the same machine as the
                 database, and the key that decrypts the wallet addresses and
                 provider credentials inside them ($REMOTE_DIR/.env,
                 SECRET_KEY) also lives only here — deliberately not copied
                 next to the dumps (AUD-426).

    Closing that gap is an owner action and cannot be automated from here
    without putting an off-host credential on this host: copy the newest
    $REMOTE_DIR/backups/audr-*.sql.* AND the SECRET_KEY value somewhere
    else, stored apart from each other. See docs/operations.md#backups.
EOF
