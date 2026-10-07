#!/usr/bin/env bash
# Send an operational alert to every channel that is configured (AUD-444).
# Reads the message from "$@", or from stdin when called with no arguments.
#
#   scripts/notify.sh "stand is down"
#   printf '%s\n' "$body" | scripts/notify.sh --key stand-down
#   scripts/notify.sh --key stand-down --resolve "back up"
#
# TWO SINKS, EITHER OR BOTH
# -------------------------
#   board     a `critical` Paperclip issue, via scripts/notify-board.py. This is
#             the channel chosen on AUD-444: it lands where the team already
#             looks and it WAKES ITS ASSIGNEE, so noticing becomes a timer's job
#             rather than someone's attention. AUD-443 was two hours of
#             nobody-noticing time, which is the failure a chat message shares.
#   telegram  one outbound HTTPS POST to api.telegram.org. Kept because it needs
#             no ingress, which matters here — the stand is on a LAN address, so
#             a hosted uptime prober would need a firewall change to reach it.
#
# Both are tried, independently. A sink with no credentials is skipped and is
# not an error, so the board sink alone is a complete configuration.
#
# --key names an ongoing condition. The board sink uses it to comment on the
# issue it already opened instead of opening a second one, and --resolve closes
# that issue. Telegram ignores both: a chat has no lifecycle to dedupe against.
#
# CREDENTIALS. This repository is public, so no credential is ever in it. Each
# sink reads its own file from the same secrets directory:
#
#   secrets/paperclip.env   PAPERCLIP_API_URL, PAPERCLIP_API_KEY,
#                           PAPERCLIP_COMPANY_ID (+ optional AUDR_ALERT_* routing)
#   secrets/telegram.env    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#
# searched as $AUDR_BOARD_ENV / $AUDR_NOTIFY_ENV first, then
# $AUDR_REMOTE_DIR/secrets/, then <repo>/secrets/ for local work. The host
# watchdog and the deploy workflow both run ON the deploy host as the same user,
# so one copy of each file serves both and there is nothing to rotate twice.
#
# UNCONFIGURED IS NOT AN ERROR. With no credentials at all this prints the
# message it would have sent and exits 0. That is deliberate: every caller here
# is already on a failure path, and an alerting helper that turns "the stand is
# down" into "the alerting helper also broke" is worse than no alerting. It also
# means a clone with no credentials still exercises every code path, and that
# activating a channel is purely dropping its file in place — no code change and
# no redeploy. Pass --strict to invert this for a connectivity test; it then
# requires every configured sink to have delivered.
set -uo pipefail

strict=0
alert_key=""
resolve=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --strict)  strict=1; shift ;;
    --resolve) resolve=1; shift ;;
    --key)     alert_key="${2:-}"; shift 2 ;;
    --key=*)   alert_key="${1#--key=}"; shift ;;
    --)        shift; break ;;
    *)         break ;;
  esac
done

if [ "$#" -gt 0 ]; then
  message="$*"
else
  message="$(cat)"
fi
[ -n "$message" ] || { echo "[notify] refusing to send an empty message" >&2; exit 2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Per-sink outcome: "" skipped (no credentials), "ok" delivered, "fail" tried
# and could not. The summary at the end needs the distinction — "nothing is
# configured" and "the channel is broken" call for different responses.
board_state=""
telegram_state=""

# ---------------------------------------------------------------- board sink
BOARD="$REPO_DIR/scripts/notify-board.py"
if [ -f "$BOARD" ]; then
  board_args=()
  [ -n "$alert_key" ] && board_args+=(--key "$alert_key")
  [ "$resolve" = 1 ] && board_args+=(--resolve)
  # Exit codes are this script's contract with notify-board.py:
  #   0 delivered (or nothing to resolve), 1 configured but failed,
  #   3 no credentials. See its header.
  python3 "$BOARD" "${board_args[@]+"${board_args[@]}"}" -- "$message"
  case "$?" in
    0) board_state=ok ;;
    3) board_state="" ;;
    *) board_state=fail ;;
  esac
else
  echo "[notify] scripts/notify-board.py missing — board sink unavailable" >&2
fi

# ------------------------------------------------------------- telegram sink
# Only consult files when the environment has not already supplied the pair.
# Same precedence as deploy-env.sh: environment first, file second.
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  for candidate in \
    "${AUDR_NOTIFY_ENV:-}" \
    "${AUDR_REMOTE_DIR:-}/secrets/telegram.env" \
    "$REPO_DIR/secrets/telegram.env"
  do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
      # shellcheck disable=SC1090
      . "$candidate"
      break
    fi
  done
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  telegram_state=fail
  # Three attempts: the thing we are usually reporting is a host in a bad state,
  # which is exactly when one outbound request is most likely to blip.
  attempt=1
  while [ "$attempt" -le 3 ]; do
    http="$(curl -s -o /tmp/audr-notify.$$ -w '%{http_code}' -m 15 \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=${message}" \
      --data-urlencode "disable_web_page_preview=true" \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" 2>/dev/null || echo 000)"
    if [ "$http" = 200 ]; then
      echo "[notify] telegram sent (${#message} chars)"
      telegram_state=ok
      break
    fi
    # Never echo the response body unredacted into a public CI log: a Telegram
    # error repeats the request, and the token is in the URL on some error paths.
    echo "[notify] telegram attempt $attempt/3 failed (HTTP $http)" >&2
    attempt=$((attempt + 1))
    [ "$attempt" -le 3 ] && sleep $((attempt * 3))
  done
  rm -f /tmp/audr-notify.$$
fi

# ------------------------------------------------------------------- summary
if [ -z "$board_state" ] && [ -z "$telegram_state" ]; then
  echo "[notify] no channel configured (see secrets/paperclip.env, secrets/telegram.env)"
  echo "[notify] message NOT sent:"
  printf '%s\n' "$message" | sed 's/^/[notify]   /'
  [ "$strict" = 1 ] && exit 1
  exit 0
fi

if [ "$board_state" != ok ] && [ "$telegram_state" != ok ]; then
  echo "[notify] GAVE UP — no configured channel accepted the alert. Message follows:" >&2
  printf '%s\n' "$message" | sed 's/^/[notify]   /' >&2
  [ "$strict" = 1 ] && exit 1
  exit 0
fi

# At least one sink delivered. Under --strict a half-delivered alert still
# counts as a failure: the whole point of the flag is proving the wiring works.
if [ "$strict" = 1 ] && { [ "$board_state" = fail ] || [ "$telegram_state" = fail ]; }; then
  echo "[notify] delivered to some channels but not all (board=${board_state:-skipped} telegram=${telegram_state:-skipped})" >&2
  exit 1
fi

echo "[notify] delivered (board=${board_state:-skipped} telegram=${telegram_state:-skipped})"
exit 0
