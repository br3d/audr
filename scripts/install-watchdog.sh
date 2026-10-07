#!/usr/bin/env bash
# Install (or re-install) the stand health watchdog on the deploy host (AUD-444).
#
#   scripts/install-watchdog.sh            install/update, then show status
#   scripts/install-watchdog.sh --check     report what is installed; no changes
#
# What it puts on the host:
#   <install dir>/scripts/{stand-watchdog.sh,notify.sh,notify-board.py}
#                                                         verbatim repo copies
#   /etc/systemd/system/audr-watchdog.service             from the .in template
#   /etc/systemd/system/audr-watchdog.timer
#
# Why copies rather than pointing the unit at a git checkout: the only checkout
# on that host belongs to the CI runner, which rewrites it on every deploy and
# resets it on a failed one. A watchdog whose code disappears during a bad
# deploy is useless at precisely the moment it is needed.
#
# Re-running is the update path and is idempotent. Changing either script in
# this repo does NOT reach the host until this runs — unlike the workflows,
# which ship with the commit since AUD-443.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/deploy-env.sh
. "$REPO_DIR/scripts/lib/deploy-env.sh"
audr_load_deploy_env "$REPO_DIR"

HOST="${AUDR_DEPLOY_HOST:-}"
audr_require AUDR_DEPLOY_HOST "SSH destination of the deploy host that should run the watchdog."
KEY="${AUDR_SSH_KEY:-$REPO_DIR/id_ed25519}"
REMOTE_DIR="${AUDR_REMOTE_DIR:-/home/codex/audr}"
INSTALL_DIR="${AUDR_WATCHDOG_DIR:-.local/share/audr-watchdog}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$HOST")

mode=apply
case "${1:-}" in
  --check) mode=check ;;
  "")      ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

if [ "$mode" = check ]; then
  "${SSH[@]}" '
    echo "== units =="
    systemctl list-unit-files audr-watchdog.\* --no-pager 2>/dev/null | head -5 || true
    echo "== timer =="
    systemctl list-timers audr-watchdog.timer --no-pager 2>/dev/null | head -3 || true
    echo "== last run =="
    systemctl status audr-watchdog.service --no-pager -n 10 2>/dev/null | tail -14 || echo "not installed"
    echo "== credentials =="
    # Either file on its own is a complete channel; both absent is the only
    # state in which a detected outage reaches nobody.
    present=0
    for f in ~/audr/secrets/paperclip.env ~/audr/secrets/telegram.env; do
      if [ -f "$f" ]; then echo "$f present ($(stat -c %a "$f"))"; present=1; else echo "$f absent"; fi
    done
    [ "$present" = 1 ] || echo "NO CHANNEL CONFIGURED — alerts will be logged on this host and delivered to nobody"
  '
  exit 0
fi

# Absolute install path, resolved on the host so ~ belongs to the remote user.
remote_home="$("${SSH[@]}" 'echo "$HOME"')"
abs_install="$remote_home/${INSTALL_DIR#"$remote_home/"}"

echo "==> installing watchdog scripts to $HOST:$abs_install/scripts"
"${SSH[@]}" "mkdir -p '$abs_install/scripts'"
scp -q -i "$KEY" -o StrictHostKeyChecking=no \
  "$REPO_DIR/scripts/stand-watchdog.sh" "$REPO_DIR/scripts/notify.sh" \
  "$REPO_DIR/scripts/notify-board.py" \
  "$HOST:$abs_install/scripts/"
"${SSH[@]}" "chmod 755 '$abs_install/scripts/stand-watchdog.sh' '$abs_install/scripts/notify.sh' '$abs_install/scripts/notify-board.py'"

echo "==> rendering and installing systemd units (sudo on the host)"
service="$(sed -e "s|@INSTALL_DIR@|$abs_install|g" -e "s|@REMOTE_DIR@|$REMOTE_DIR|g" \
  "$REPO_DIR/ci/host-units/audr-watchdog.service.in")"

# Units go in via stdin rather than scp-to-/etc: the SSH user has no write
# access there, and this keeps the privileged step to one explicit sudo tee.
printf '%s\n' "$service" | "${SSH[@]}" 'sudo tee /etc/systemd/system/audr-watchdog.service >/dev/null'
"${SSH[@]}" 'sudo tee /etc/systemd/system/audr-watchdog.timer >/dev/null' \
  < "$REPO_DIR/ci/host-units/audr-watchdog.timer"

"${SSH[@]}" 'sudo systemctl daemon-reload && sudo systemctl enable --now audr-watchdog.timer'

echo "==> first probe"
"${SSH[@]}" 'sudo systemctl start audr-watchdog.service; systemctl status audr-watchdog.service --no-pager -n 15 | tail -12'

echo
echo "==> installed. Timer:"
"${SSH[@]}" 'systemctl list-timers audr-watchdog.timer --no-pager | head -3'
echo
"${SSH[@]}" "test -f '$REMOTE_DIR/secrets/paperclip.env' -o -f '$REMOTE_DIR/secrets/telegram.env'" \
  && echo "==> Alert credentials present — alerts will be delivered." \
  || cat <<EOF
==> NOTE: neither $REMOTE_DIR/secrets/paperclip.env nor telegram.env exists, so
    the watchdog is running and will log alerts but deliver them to nobody.

    The channel chosen on AUD-444 is the board: a failing stand opens a
    \`critical\` Paperclip issue, which wakes its assignee rather than waiting
    for someone to be watching a chat. To activate it:

      1. Have a board user mint an agent API key with scope
         {"kind":"task_bridge"} — scoped to the audr project, and with
         allowedAssigneeAgentIds limited to the agent that should be woken.
         Agents cannot mint their own keys, so this step needs a human.
      2. On the deploy host:
           umask 077
           cat > $REMOTE_DIR/secrets/paperclip.env <<'CREDS'
           PAPERCLIP_API_URL=https://<paperclip-host>
           PAPERCLIP_API_KEY=<the task_bridge key>
           PAPERCLIP_COMPANY_ID=<company uuid>
           AUDR_ALERT_ASSIGNEE_AGENT_ID=<agent uuid to wake>
           AUDR_ALERT_PROJECT_ID=<project uuid>
           CREDS
      3. Verify:  $abs_install/scripts/notify.sh --strict "audr alerting test"
                  (opens a real issue — close it afterwards)

    Telegram remains supported as a second, independent sink; drop
    TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID into secrets/telegram.env for it.

    No redeploy and no code change is needed — both the watchdog and the deploy
    workflow pick the file up on their next run.
EOF
