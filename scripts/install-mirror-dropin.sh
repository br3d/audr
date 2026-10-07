#!/usr/bin/env bash
# Install (or re-install) the audr-mirror boot-DNS-race drop-in on the deploy
# host.
#
#   scripts/install-mirror-dropin.sh           install/update, then verify
#   scripts/install-mirror-dropin.sh --check    report what is installed; no changes
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

HOST="${AUDR_DEPLOY_HOST:-}"
audr_require AUDR_DEPLOY_HOST "SSH destination of the deploy host that runs the GitHub->Gitea mirror."
KEY="${AUDR_SSH_KEY:-$REPO_DIR/id_ed25519}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$HOST")

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
    exit 0
    ;;
  "") ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

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
