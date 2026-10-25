#!/usr/bin/env bash
# Install (or re-install) the audr-mirror boot-DNS-race drop-in on the deploy
# host.
#
#   scripts/install-mirror-dropin.sh           install/update, then verify
#   scripts/install-mirror-dropin.sh --check-drift  compare the host's pinned copies against
#                                      this checkout and print one line per file; no
#                                      changes, no sink probes, one SSH round trip.
#                                      Non-zero on drift. This is what the deploy
#                                      workflow runs every deploy (AUD-698).
#   scripts/install-mirror-dropin.sh --check    report what is installed, and
#                                               whether the host's copy still
#                                               matches this checkout; no changes.
#                                               EXITS NON-ZERO ON DRIFT (AUD-698)
#                                               so a workflow can gate on it —
#                                               see scripts/host-install-drift.sh.
#
# Env: AUDR_DEPLOY_HOST  ssh destination of the host, or `local` to inspect the
#                        machine this runs on (read-only modes only — that is
#                        how the deploy workflow, which runs ON the host, checks
#                        for drift without an ssh hop).
#
# What it puts on the host:
#   /etc/systemd/system/audr-mirror.service.d/10-dns-race.conf
#
# Why only the drop-in and not the whole unit: `audr-mirror.service` and its
# timer are host-only and hold the host's layout (the mirror script path, the
# Gitea push URL's directory). This repository is public, so the unit stays off
# it. The drop-in references nothing but `github.com` and is safe to track.
#
# Re-running is the update path and is idempotent. Unlike the workflows, which
# ship with the commit since AUD-443, a change to the tracked `.conf` does NOT
# reach the host until this runs.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/deploy-env.sh
. "$REPO_DIR/scripts/lib/deploy-env.sh"
audr_load_deploy_env "$REPO_DIR"
# AUDR_MIRROR_FILES — the single source of truth for what this script pins to
# the host, used by the drift check below (AUD-698).
# shellcheck source=scripts/lib/host-install-manifest.sh
. "$REPO_DIR/scripts/lib/host-install-manifest.sh"

HOST="${AUDR_DEPLOY_HOST:-}"
KEY="${AUDR_SSH_KEY:-$REPO_DIR/id_ed25519}"
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
  audr_require AUDR_DEPLOY_HOST "SSH destination of the deploy host that runs the GitHub->Gitea mirror."
  # accept-new, not no: both learn an unknown host key without prompting, but
  # `no` ALSO accepts a key that has CHANGED since last time, which is the one
  # case worth stopping for. Matches scripts/deploy.sh (AUD-698 item 3).
  SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$HOST")
fi

DROPIN="$REPO_DIR/ci/host-units/audr-mirror-dns-race.conf"
REMOTE_DROPIN=/etc/systemd/system/audr-mirror.service.d/10-dns-race.conf

case "${1:-}" in
  --check)
    "${SSH[@]}" "
      echo '== drop-in =='
      if [ -f $REMOTE_DROPIN ]; then
        echo '$REMOTE_DROPIN present'
      else
        echo '$REMOTE_DROPIN ABSENT — a reboot will leave the mirror unit failed'
      fi
      echo '== as systemd parsed it =='
      systemctl show audr-mirror.service -p ExecStartPre -p Restart -p RestartUSec
      echo '== last run =='
      systemctl show audr-mirror.service -p Result -p ExecMainStatus
      journalctl -u audr-mirror.service -n 5 --no-pager 2>/dev/null | tail -5 || true
    "

    # Content drift (AUD-698). `present` above is not `current`: the whole point
    # of the drop-in is that it makes the mirror unit survive a boot-time DNS
    # race, and a host carrying last month's version of it survives last
    # month's race. This is also the closest analogue in the tree to AUD-443
    # itself — that outage was the host's copies of CI *workflow* files falling
    # behind the repo's with nothing to notice. Workflows ship with the commit
    # now; this file cannot, so it is measured instead.
    echo '== installed copy vs this checkout =='
    drift_rc=0
    audr_drift_report "$REPO_DIR" "" "" "${AUDR_MIRROR_FILES[@]}" || drift_rc=$?
    if [ "$drift_rc" -ne 0 ]; then
      echo
      echo "==> DRIFT: the host's drop-in is not this checkout's. Re-run: $0"
    fi
    exit "$drift_rc"
    ;;
  # The content comparison alone: the only part of --check with an exit code and
  # the only part cheap enough to run on every deploy (AUD-698).
  --check-drift)
    audr_drift_report "$REPO_DIR" "" "" "${AUDR_MIRROR_FILES[@]}"
    exit $?
    ;;
  "") ;;
  *) echo "usage: $0 [--check|--check-drift]" >&2; exit 2 ;;
esac

# A local install is not supported; see the AUDR_DEPLOY_HOST=local note above.
if [ "${AUDR_DEPLOY_HOST:-}" = local ]; then
  echo "ERROR: AUDR_DEPLOY_HOST=local supports the read-only check modes only." >&2
  echo "       Installing needs a real destination: scp and the units' \`sudo tee\`" >&2
  echo "       are not the same operation against localhost, and guessing which" >&2
  echo "       you meant is worse than refusing. Re-run with --check-drift, or" >&2
  echo "       set AUDR_DEPLOY_HOST to the host's ssh destination." >&2
  exit 2
fi

echo "==> installing drop-in on $HOST (sudo on the host)"
# Via stdin rather than scp-to-/etc: the SSH user has no write access there, and
# this keeps the privileged step to one explicit sudo tee.
"${SSH[@]}" "sudo mkdir -p $(dirname "$REMOTE_DROPIN") && sudo tee $REMOTE_DROPIN >/dev/null" < "$DROPIN"
"${SSH[@]}" 'sudo systemctl daemon-reload && sudo systemctl reset-failed audr-mirror.service || true'

echo "==> running one sync to prove the unit still works with the drop-in applied"
"${SSH[@]}" 'sudo systemctl start audr-mirror.service'

echo "==> result"
"${SSH[@]}" '
  systemctl show audr-mirror.service -p Result -p ExecMainStatus -p ExecStartPre | sed "s/ ; /\n    /g"
  journalctl -u audr-mirror.service -n 4 --no-pager 2>/dev/null | tail -4 || true
'
