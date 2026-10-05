#!/usr/bin/env bash
# Send an operational alert to the team channel. Reads the message from "$@",
# or from stdin when called with no arguments.
#
#   scripts/notify.sh "stand is down"
#   printf '%s\n' "$body" | scripts/notify.sh
#
# The channel is Telegram (AUD-444). It was chosen over email, Slack and a
# hosted uptime prober for one structural reason: the stand lives on a LAN
# address, and Telegram is the only one of those that needs no ingress — a
# single outbound HTTPS POST to api.telegram.org and nothing listening.
#
# CREDENTIALS. Two values, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. This
# repository is public, so they are never in it. They are read from the
# environment, or from the first credentials file that exists:
#
#   $AUDR_NOTIFY_ENV                     explicit override
#   $AUDR_REMOTE_DIR/secrets/telegram.env   the deploy host's own secrets dir
#   <repo>/secrets/telegram.env          local/dev, untracked (see .gitignore)
#
# Both the host watchdog and the deploy workflow run ON the deploy host, so
# that one file serves both and there is no second copy of the token to rotate.
#
# UNCONFIGURED IS NOT AN ERROR. With no token the script prints the message it
# would have sent and exits 0. That is deliberate: every caller here is already
# on a failure path, and an alerting helper that turns "the stand is down" into
# "the alerting helper also broke" is worse than no alerting. It means a clone
# with no credentials still runs every code path, and that activating the
# channel is purely dropping the file in place — no code change, no redeploy.
# Pass --strict to invert this for a connectivity test.
set -uo pipefail

strict=0
case "${1:-}" in
  --strict) strict=1; shift ;;
esac

if [ "$#" -gt 0 ]; then
  message="$*"
else
  message="$(cat)"
fi
[ -n "$message" ] || { echo "[notify] refusing to send an empty message" >&2; exit 2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "[notify] no channel configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset)"
  echo "[notify] message NOT sent:"
  printf '%s\n' "$message" | sed 's/^/[notify]   /'
  [ "$strict" = 1 ] && exit 1
  exit 0
fi

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
    rm -f /tmp/audr-notify.$$
    echo "[notify] sent (${#message} chars)"
    exit 0
  fi
  # Never echo the response body unredacted into a public CI log: a Telegram
  # error repeats the request, and the token is in the URL on some error paths.
  echo "[notify] attempt $attempt/3 failed (HTTP $http)" >&2
  attempt=$((attempt + 1))
  [ "$attempt" -le 3 ] && sleep $((attempt * 3))
done

rm -f /tmp/audr-notify.$$
echo "[notify] GAVE UP — alert was not delivered. Message follows:" >&2
printf '%s\n' "$message" | sed 's/^/[notify]   /' >&2
[ "$strict" = 1 ] && exit 1
exit 0
