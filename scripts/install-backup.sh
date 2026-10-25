#!/usr/bin/env bash
# Install (or re-install) the scheduled encrypted database backup on the deploy
# host (AUD-562).
#
#   scripts/install-backup.sh            install/update, take a first dump, show status
#   scripts/install-backup.sh --check-drift  compare the host's pinned copies against
#                               this checkout and print one line per file; no
#                               changes, no sink probes, one SSH round trip.
#                               Non-zero on drift. This is what the deploy
#                               workflow runs every deploy (AUD-698).
#   scripts/install-backup.sh --check    report what is installed, and whether the
#                                        host's pinned copies still match this
#                                        checkout; no changes. EXITS NON-ZERO ON
#                                        DRIFT (AUD-698), so a workflow can gate
#                                        on it — see scripts/host-install-drift.sh.
#
# Env: AUDR_DEPLOY_HOST  ssh destination of the host, or `local` to inspect the
#                        machine this runs on (read-only modes only — that is
#                        how the deploy workflow, which runs ON the host, checks
#                        for drift without an ssh hop).
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
# AUDR_BACKUP_FILES — the single source of truth for what this script pins to
# the host, used below to drive both the copy and the drift check so the two
# cannot disagree (AUD-698).
# shellcheck source=scripts/lib/host-install-manifest.sh
. "$REPO_DIR/scripts/lib/host-install-manifest.sh"

HOST="${AUDR_DEPLOY_HOST:-}"
KEY="${AUDR_SSH_KEY:-$REPO_DIR/id_ed25519}"
REMOTE_DIR="${AUDR_REMOTE_DIR:-/home/codex/audr}"
INSTALL_DIR="${AUDR_BACKUP_INSTALL_DIR:-.local/share/audr-backup}"
# AUDR_DEPLOY_HOST=local means "inspect THIS machine" (AUD-698).
#
# The deploy workflow's job is `runs-on: host` — it executes on the deploy host
# itself, which is why it drives `docker compose` directly and never ssh's
# anywhere. There is consequently no AUDR_DEPLOY_HOST set in that job and no key
# configured to make a hop with, so a drift check that insisted on ssh would
# exit "could not check" on every deploy: a step that looks like a guard and
# measures nothing.
#
# `local` substitutes a local shell for the ssh invocation. Every command this
# script sends is a string run by `"${SSH[@]}"`, so a plain `bash -c` satisfies
# the same contract — the same substitution backend/tests/unit/
# test_host_install_drift.py uses to exercise the comparison, and
# `"${SSH[@]}" 'echo "$HOME"'` resolves the install directory identically.
#
# Read-only modes only. A local *install* is refused further down: scp-to-self
# and the `sudo tee` of a unit are different enough operations that quietly
# doing something not-quite-that would be worse than declining.
if [ "${AUDR_DEPLOY_HOST:-}" = local ]; then
  SSH=(bash -c)
else
  audr_require AUDR_DEPLOY_HOST "SSH destination of the deploy host that should back itself up."
  # accept-new, not no: both learn an unknown host key without prompting, but
  # `no` ALSO accepts a key that has CHANGED since last time, which is the one
  # case worth stopping for. Matches scripts/deploy.sh (AUD-698 item 3).
  SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$HOST")
fi

mode=apply
case "${1:-}" in
  --check)       mode=check ;;
  --check-drift) mode=drift ;;
  "")            ;;
  *) echo "usage: $0 [--check|--check-drift]" >&2; exit 2 ;;
esac

# Needed by --check as well as by the install since AUD-698: the drift report
# compares the host's pinned copies, and those live under this directory.
# Absolute install path, resolved on the host so ~ belongs to the remote user.
# shellcheck disable=SC2016  # $HOME must reach the REMOTE shell unexpanded —
# that is the entire point of the line. Double quotes would resolve it here, on
# the agent host, and install under the wrong user's home.
remote_home="$("${SSH[@]}" 'echo "$HOME"')"
abs_install="$remote_home/${INSTALL_DIR#"$remote_home/"}"

# --check-drift: the content comparison alone, which is the only part of --check
# with an exit code and the only part cheap enough to run on every deploy. The
# full --check also lists units, timers and dumps and (for the watchdog) probes
# each alert sink over the network — useful to a human at a terminal, noise in a
# workflow, and a probe that costs an outbound HTTPS call per run.
if [ "$mode" = drift ]; then
  audr_drift_report "$REPO_DIR" "$abs_install" "$REMOTE_DIR" "${AUDR_BACKUP_FILES[@]}"
  exit $?
fi

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
    echo '== compression and encryption =='
    # gzip before age (AUD-564). Report it as its own line: a host without
    # gzip takes dumps 5.7x bigger than the retention was sized for, and the
    # only other place that shows up is the disk filling.
    if command -v gzip >/dev/null 2>&1; then
      echo \"gzip present (\$(gzip --version 2>&1 | head -1)) — dumps are compressed before encryption\"
    else
      echo 'gzip ABSENT — backup.sh refuses to run unless AUDR_BACKUP_COMPRESS=none'
    fi
    if command -v age >/dev/null 2>&1; then
      echo \"age present (\$(age --version 2>&1 | head -1)) — dumps are .sql.gz.age\"
    elif command -v gpg >/dev/null 2>&1; then
      echo 'age ABSENT, gpg present — dumps take the .sql.gz.gpg fallback path'
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

  # Content drift (AUD-698). Everything above reports STATE — units, timer,
  # dumps, engine, KEK — and every line of it can be green while the host runs
  # a backup script three commits behind this checkout. That is not a
  # hypothetical: pinning the copies is deliberate (the host's only git checkout
  # is the CI runner's, rewritten every deploy), and the price of pinning is
  # that a repo edit reaches the host only when this script runs again.
  #
  # So this section asks the one question the rest does not: are the bytes on
  # the host the bytes in this commit? It is also the only part of --check with
  # an exit code attached, which is what lets a workflow gate on it.
  echo '== pinned copies vs this checkout =='
  drift_rc=0
  audr_drift_report "$REPO_DIR" "$abs_install" "$REMOTE_DIR" "${AUDR_BACKUP_FILES[@]}" || drift_rc=$?
  if [ "$drift_rc" -ne 0 ]; then
    echo
    echo "==> DRIFT: the host's backup code is not this checkout's. Re-run: $0"
  fi
  exit "$drift_rc"
fi


# A local install is not supported; see the AUDR_DEPLOY_HOST=local note above.
if [ "${AUDR_DEPLOY_HOST:-}" = local ]; then
  echo "ERROR: AUDR_DEPLOY_HOST=local supports the read-only check modes only." >&2
  echo "       Installing needs a real destination: scp and the units' \`sudo tee\`" >&2
  echo "       are not the same operation against localhost, and guessing which" >&2
  echo "       you meant is worse than refusing. Re-run with --check-drift, or" >&2
  echo "       set AUDR_DEPLOY_HOST to the host's ssh destination." >&2
  exit 2
fi

echo "==> installing backup scripts to $HOST:$abs_install/scripts"
"${SSH[@]}" "mkdir -p '$abs_install/scripts'"
# Both the file list and the chmod list come from AUDR_BACKUP_FILES, so the
# copy and the --check above cannot disagree about what "installed" means — a
# file dropped from one list and not the other is precisely the drift this
# ticket is about, one level up (AUD-698).
#
# notify.sh resolves its sibling notify-board.py relative to its own directory,
# so both travel together or alerting silently degrades to a log line. The
# manifest keeps them adjacent for that reason.
mapfile -t pinned_sources < <(audr_manifest_scp_sources "$REPO_DIR" "$abs_install" "${AUDR_BACKUP_FILES[@]}")
mapfile -t pinned_targets < <(audr_manifest_remote_targets "$abs_install" "${AUDR_BACKUP_FILES[@]}")
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new \
  "${pinned_sources[@]}" "$HOST:$abs_install/scripts/"
"${SSH[@]}" "chmod 755 $(printf "'%s' " "${pinned_targets[@]}")"

echo "==> checking the host can encrypt"
if ! "${SSH[@]}" 'command -v age >/dev/null 2>&1'; then
  if "${SSH[@]}" 'command -v gpg >/dev/null 2>&1'; then
    echo "    NOTE: 'age' is not installed; dumps will take the gpg --symmetric"
    echo "    fallback (.sql.gz.gpg). Both are AES-256 and restore.sh reads either."
    echo "    'age' is preferred and is in the Ubuntu archive: sudo apt install age"
  else
    echo "ERROR: the host has neither 'age' nor 'gpg' — backup.sh cannot encrypt." >&2
    echo "       Install one (sudo apt install age) and re-run." >&2
    exit 1
  fi
fi

echo "==> rendering and installing systemd units (sudo on the host)"
# Via audr_render_unit so the substitution the install applies is literally the
# same code the --check above renders with. Two copies of one sed would drift,
# and a check that rendered differently would report permanent drift on a
# correctly installed host — an alert that always fires is one nobody reads.
service="$(audr_render_unit "$REPO_DIR/ci/host-units/audr-backup.service.in" "$abs_install" "$REMOTE_DIR")"

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
