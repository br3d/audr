#!/usr/bin/env bash
# Install (or re-install) the stand health watchdog on the deploy host (AUD-444).
#
#   scripts/install-watchdog.sh            install/update, then show status
#   scripts/install-watchdog.sh --check     report what is installed; no changes
#
# What it puts on the host:
#   <install dir>/scripts/{stand-watchdog.sh,notify.sh}   verbatim repo copies
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
    for f in ~/audr/secrets/telegram.env; do
      if [ -f "$f" ]; then echo "$f present ($(stat -c %a "$f"))"; else echo "$f ABSENT — alerts will log only"; fi
    done
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
  "$HOST:$abs_install/scripts/"
"${SSH[@]}" "chmod 755 '$abs_install/scripts/stand-watchdog.sh' '$abs_install/scripts/notify.sh'"

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
"${SSH[@]}" "test -f '$REMOTE_DIR/secrets/telegram.env'" \
  && echo "==> Telegram credentials present — alerts will be delivered." \
  || cat <<EOF
==> NOTE: $REMOTE_DIR/secrets/telegram.env is absent, so the watchdog is
    running and will log alerts but not deliver them. To activate the channel:

      1. Create a bot with @BotFather, keep the token.
      2. Send it a message; read the chat id from
         https://api.telegram.org/bot<TOKEN>/getUpdates
      3. On the deploy host:
           umask 077
           cat > $REMOTE_DIR/secrets/telegram.env <<'CREDS'
           TELEGRAM_BOT_TOKEN=...
           TELEGRAM_CHAT_ID=...
           CREDS
      4. Verify:  $abs_install/scripts/notify.sh --strict "audr alerting test"

    No redeploy and no code change is needed — both the watchdog and the deploy
    workflow pick the file up on their next run.
EOF
