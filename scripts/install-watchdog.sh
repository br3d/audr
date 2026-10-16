#!/usr/bin/env bash
# Install (or re-install) the stand's host-side alerting on the deploy host
# (AUD-444; extended with news alert dispatch by AUD-584).
#
#   scripts/install-watchdog.sh            install/update, then show status
#   scripts/install-watchdog.sh --check     report what is installed; no changes
#
# What it puts on the host:
#   <install dir>/scripts/{stand-watchdog.sh,alert-dispatch.sh,notify.sh,
#                          notify-board.py}               verbatim repo copies
#   /etc/systemd/system/audr-watchdog.service             from the .in template
#   /etc/systemd/system/audr-watchdog.timer
#   /etc/systemd/system/audr-alert-dispatch.service       from the .in template
#   /etc/systemd/system/audr-alert-dispatch.timer
#
# Two timers, one installer: both are host-side delivery over the same notify.sh
# and the same secrets directory, so a second install script would only create a
# way to end up with half of it installed. Why they are nevertheless separate
# systemd units is argued in ci/host-units/audr-alert-dispatch.timer.
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

# --- sink probes (AUD-594) ----------------------------------------------------
# This script used to end with "Alert credentials present — alerts will be
# delivered." on the strength of `test -f secrets/paperclip.env`. Both the file
# and that line were there on the stand, and the claim was false: the key inside
# had expired, so every alert spent a 401 and then spooled. Delivery did still
# happen, by the slow spool->drain path AUD-591 designed on purpose — but an
# operator who read that line had been told the host delivers directly, inside
# the probe window. The one sentence summarising the whole install was wrong
# about the only thing it claimed, which is the same false-green shape as a
# green unit doing impossible work (AUD-584) and is read the same way: as a
# reason to stop looking.
#
# So both probes ask the channel, not the filesystem, and report three states
# rather than two — "not configured" and "configured and refused" need different
# actions. Exit codes are notify-board.py's contract:
#   0 live   1 configured but refused/unreachable   3 not configured
probe_board() {
  # Probes the *installed* copy, which is the one the timers execute. Running
  # the repo copy instead would prove a file that is not on the delivery path.
  if ! "${SSH[@]}" "test -f '$1/scripts/notify-board.py'" 2>/dev/null; then
    echo "[board] $1/scripts/notify-board.py is not installed"
    return 3
  fi
  local rc=0
  "${SSH[@]}" "AUDR_REMOTE_DIR='$REMOTE_DIR' python3 '$1/scripts/notify-board.py' --probe" || rc=$?
  # 2 is argparse's usage error, which here means one specific thing: the copy
  # on the host predates --probe. Worth naming, because the alternative is an
  # operator reading "did not answer" and going to look at the credential.
  if [ "$rc" = 2 ]; then
    echo "[board] the installed notify-board.py predates --probe (AUD-594) — re-run this installer to update it"
  fi
  return "$rc"
}

probe_telegram() {
  # `bash -s` with the directory as $1: the alternative is nesting three levels
  # of quoting inside a remote one-liner, which is how a path with a space
  # becomes a probe that silently tests nothing.
  "${SSH[@]}" "bash -s -- '$REMOTE_DIR'" <<'REMOTE'
set -u
env_file="$1/secrets/telegram.env"
[ -f "$env_file" ] || { echo "[telegram] $env_file absent"; exit 3; }

# Reads the two names rather than sourcing the file: it holds a bot token, and
# `.` would execute whatever else ended up in there. Same rule as
# notify-board.py's load_env_file, for the same reason.
field() {
  sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?$1=//p" "$env_file" \
    | tail -1 | tr -d "\"'" | tr -d '\r'
}
token="$(field TELEGRAM_BOT_TOKEN)"
chat="$(field TELEGRAM_CHAT_ID)"
if [ -z "$token" ] || [ -z "$chat" ]; then
  echo "[telegram] $env_file exists but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not both set"
  exit 1
fi

# getMe, not sendMessage: it proves the token without putting a test message in
# the owner's chat on every install. chat_id cannot be verified without sending,
# so it is checked for presence only — and the report says so rather than
# implying more than was checked.
code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 \
  "https://api.telegram.org/bot${token}/getMe" 2>/dev/null || echo 000)"
case "$code" in
  200) echo "[telegram] bot token answers getMe (HTTP 200); chat_id is set, unverifiable without sending"; exit 0 ;;
  000) echo "[telegram] no answer from api.telegram.org — token not verified"; exit 1 ;;
  # The token is never echoed: it travels in the request URL, and this output
  # lands in terminals and CI logs.
  *)   echo "[telegram] bot token REJECTED (HTTP $code)"; exit 1 ;;
esac
REMOTE
}

# Renders the line this script is judged on, saying only what the probes
# support. Returns 1 when nothing is configured at all, which is the one case
# that needs the long setup note instead of a verdict.
report_sinks() {
  local board_rc="$1" tg_rc="$2"
  local live=() broken=()
  # Anything that is neither 0 nor 3 counts as broken, not as unconfigured.
  # The codes are a contract, but a probe can still fail in a way outside it —
  # an installed copy too old to know --probe exits 2 — and defaulting an
  # unrecognised code to "no credentials here" would print the cheerful setup
  # note over a channel that exists and is failing. Unknown resolves towards
  # the louder reading on purpose.
  classify() {
    case "$2" in
      0) live+=("$1") ;;
      3) ;;
      *) broken+=("$1") ;;
    esac
  }
  classify board "$board_rc"
  classify telegram "$tg_rc"

  if [ "${#live[@]}" -gt 0 ]; then
    echo "==> Alerts will be delivered — probed sink(s): ${live[*]}"
    if [ "${#broken[@]}" -gt 0 ]; then
      cat <<EOF
    NOTE: configured here and did NOT answer: ${broken[*]}. Alerting works
    through the probed sink(s) above, so nothing is lost — but that channel is
    dead weight until its credential is replaced.
EOF
    fi
    return 0
  fi

  if [ "${#broken[@]}" -gt 0 ]; then
    cat <<EOF
==> WARNING: configured and did NOT answer: ${broken[*]}. No sink is live, so
    alerts are NOT delivered from this host.

    They are not lost either: notify.sh spools every undelivered alert to
    ~/.local/state/audr-notify/undelivered.jsonl and scripts/drain-alert-spool.py
    replays it from an agent run. That is the designed fallback — AUD-591 settled
    that the deploy host does not hold a board credential and should not — but it
    is the SLOW path. Nothing moves until an agent drains the spool, which can be
    up to an hour. Do not read this install as "the host pages someone in
    minutes".

    An expired agent token is the usual cause and looks exactly like this: the
    probe above reports HTTP 401. Replacing it needs a board user, because agents
    cannot mint their own keys — the format is in the setup note this script
    prints when no credential file exists at all.
EOF
    return 0
  fi
  return 1
}

mode=apply
case "${1:-}" in
  --check) mode=check ;;
  "")      ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

if [ "$mode" = check ]; then
  "${SSH[@]}" '
    echo "== units =="
    systemctl list-unit-files "audr-watchdog.*" "audr-alert-dispatch.*" --no-pager 2>/dev/null | head -8 || true
    echo "== timers =="
    systemctl list-timers audr-watchdog.timer audr-alert-dispatch.timer --no-pager 2>/dev/null | head -4 || true
    # `systemctl status` of a missing unit exits 4 and prints nothing, and the
    # exit code belongs to the pipeline, not to systemctl — so the absence has
    # to be tested for explicitly. An empty section reads like "ran and said
    # nothing", which is the opposite of "the unit does not exist".
    for unit in audr-watchdog audr-alert-dispatch; do
      echo "== last $unit run =="
      if systemctl cat "$unit.service" >/dev/null 2>&1; then
        systemctl status "$unit.service" --no-pager -n 10 2>/dev/null | tail -14
      else
        echo "not installed"
      fi
    done
    echo "== credential files =="
    for f in ~/audr/secrets/paperclip.env ~/audr/secrets/telegram.env; do
      if [ -f "$f" ]; then echo "$f present ($(stat -c %a "$f"))"; else echo "$f absent"; fi
    done
  '
fi

# Absolute install path, resolved on the host so ~ belongs to the remote user.
# Needed by --check too since AUD-594: the board probe runs the installed copy
# of notify-board.py, so it has to know where that is.
remote_home="$("${SSH[@]}" 'echo "$HOME"')"
abs_install="$remote_home/${INSTALL_DIR#"$remote_home/"}"

if [ "$mode" = check ]; then
  # Presence was listed above because it is a fact worth seeing; it is
  # deliberately NOT the verdict. A present file whose key has expired is the
  # state this whole section was rewritten for (AUD-594), and the old code read
  # it as a configured channel.
  echo "== sinks (probed, nothing sent) =="
  check_board_rc=0; probe_board "$abs_install" || check_board_rc=$?
  check_tg_rc=0;    probe_telegram || check_tg_rc=$?
  report_sinks "$check_board_rc" "$check_tg_rc" \
    || echo "==> NO CHANNEL CONFIGURED — alerts are logged on this host and delivered to nobody"
  exit 0
fi

echo "==> installing host alerting scripts to $HOST:$abs_install/scripts"
"${SSH[@]}" "mkdir -p '$abs_install/scripts'"
scp -q -i "$KEY" -o StrictHostKeyChecking=no \
  "$REPO_DIR/scripts/stand-watchdog.sh" "$REPO_DIR/scripts/alert-dispatch.sh" \
  "$REPO_DIR/scripts/notify.sh" "$REPO_DIR/scripts/notify-board.py" \
  "$HOST:$abs_install/scripts/"
"${SSH[@]}" "chmod 755 '$abs_install/scripts/stand-watchdog.sh' '$abs_install/scripts/alert-dispatch.sh' '$abs_install/scripts/notify.sh' '$abs_install/scripts/notify-board.py'"

echo "==> rendering and installing systemd units (sudo on the host)"
render_unit() {
  sed -e "s|@INSTALL_DIR@|$abs_install|g" -e "s|@REMOTE_DIR@|$REMOTE_DIR|g" "$1"
}

# Units go in via stdin rather than scp-to-/etc: the SSH user has no write
# access there, and this keeps the privileged step to one explicit sudo tee.
render_unit "$REPO_DIR/ci/host-units/audr-watchdog.service.in" \
  | "${SSH[@]}" 'sudo tee /etc/systemd/system/audr-watchdog.service >/dev/null'
"${SSH[@]}" 'sudo tee /etc/systemd/system/audr-watchdog.timer >/dev/null' \
  < "$REPO_DIR/ci/host-units/audr-watchdog.timer"
render_unit "$REPO_DIR/ci/host-units/audr-alert-dispatch.service.in" \
  | "${SSH[@]}" 'sudo tee /etc/systemd/system/audr-alert-dispatch.service >/dev/null'
"${SSH[@]}" 'sudo tee /etc/systemd/system/audr-alert-dispatch.timer >/dev/null' \
  < "$REPO_DIR/ci/host-units/audr-alert-dispatch.timer"

"${SSH[@]}" 'sudo systemctl daemon-reload && sudo systemctl enable --now audr-watchdog.timer audr-alert-dispatch.timer'

echo "==> first probe"
"${SSH[@]}" 'sudo systemctl start audr-watchdog.service; systemctl status audr-watchdog.service --no-pager -n 15 | tail -12'

echo "==> first alert-dispatch tick"
# Not --dry-run: a pending critical alert is meant to be delivered, and the
# install is the moment to find out whether it can be. With no pending alerts
# this is a no-op that proves the unit, the compose exec and the CLI point.
"${SSH[@]}" 'sudo systemctl start audr-alert-dispatch.service; systemctl status audr-alert-dispatch.service --no-pager -n 15 | tail -12'

echo
echo "==> installed. Timers:"
"${SSH[@]}" 'systemctl list-timers audr-watchdog.timer audr-alert-dispatch.timer --no-pager | head -4'
echo
echo "==> probing the alert sinks (reads only; nothing is sent)"
board_rc=0; probe_board "$abs_install" || board_rc=$?
tg_rc=0;    probe_telegram || tg_rc=$?
echo
report_sinks "$board_rc" "$tg_rc" || cat <<EOF
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
      3. Verify:  $0 --check
                  (probes the credential read-only and reports whether it
                  answers; re-running this installer prints the same verdict)

         For an end-to-end check that the issue really lands:
                  $abs_install/scripts/notify.sh --strict "audr alerting test"
                  (opens a real issue — close it afterwards)

    Telegram remains supported as a second, independent sink; drop
    TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID into secrets/telegram.env for it.

    No redeploy and no code change is needed — both the watchdog and the deploy
    workflow pick the file up on their next run.
EOF
