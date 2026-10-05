#!/usr/bin/env bash
# Probe the stand's /health/ready and alert on a sustained outage (AUD-444).
#
# Runs on the deploy host from audr-watchdog.timer, once a minute. One tick =
# one probe; all the state between ticks is a counter in a file.
#
# WHY THIS EXISTS SEPARATELY FROM CI. The deploy workflow's "Report stand state"
# step only speaks when a deploy fails, and only to whoever opens the run. The
# AUD-443 outage was two hours of nobody-noticing time, and an outage that
# starts with no deploy involved — OOM, a reboot, a dead Postgres — produces no
# CI run at all and is structurally invisible to anything inside the pipeline.
# This timer is the part that covers that case.
#
# THRESHOLD. Alerts fire after FAIL_THRESHOLD consecutive failed probes, not
# the first. A single failed probe is routinely just a container restarting
# mid-deploy; paging on it would train the one person on call to ignore the
# channel, which costs more than the five minutes it saves. Five probes at 60s
# is ~5 minutes of sustained unavailability, comfortably inside the 15-minute
# response target and far inside the two hours that prompted this.
#
# Exits 0 on an unhealthy stand. A failing probe is the normal, expected
# outcome this script is written to handle, not an error in the script; letting
# it exit non-zero would only fill the journal with unit failures that say
# nothing the alert has not already said. It exits non-zero only when it cannot
# do its job at all (unwritable state directory).
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PROBE_URL="${AUDR_PROBE_URL:-http://localhost:80/health/ready}"
FAIL_THRESHOLD="${AUDR_FAIL_THRESHOLD:-5}"
# Having alerted once, stay quiet for an hour before repeating. Silence after
# the first message reads identically to "it recovered"; a repeat every tick
# would be 60 messages an hour and get muted.
REMIND_EVERY="${AUDR_REMIND_EVERY:-60}"
STATE_DIR="${AUDR_WATCHDOG_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/audr-watchdog}"
STATE_FILE="$STATE_DIR/consecutive-failures"
NOTIFY="$REPO_DIR/scripts/notify.sh"

mkdir -p "$STATE_DIR" || { echo "[watchdog] cannot create $STATE_DIR" >&2; exit 1; }

fails=0
[ -f "$STATE_FILE" ] && fails="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
case "$fails" in ''|*[!0-9]*) fails=0 ;; esac

ready="$(curl -s -m 10 "$PROBE_URL" 2>/dev/null || true)"
stamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

case "$ready" in
  *'"status":"ok"'*)
    # Recovery is only worth a message if we actually alerted. Crossing the
    # threshold is what put someone on the hook; telling them they can stand
    # down is the other half of that, and without it the only way to know is to
    # go and look — the exact habit this whole ticket is trying to remove.
    if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
      "$NOTIFY" "$(printf '%s\n' \
        "✅ audr stand RECOVERED" \
        "" \
        "/health/ready is returning ok again at ${stamp}." \
        "Outage spanned at least ${fails} consecutive probes (~${fails} min)." \
        "No action needed.")"
      echo "[watchdog] recovered after $fails consecutive failures — recovery alert sent"
    fi
    echo 0 > "$STATE_FILE"
    exit 0
    ;;
esac

fails=$((fails + 1))
echo "$fails" > "$STATE_FILE"

# Fire on the crossing tick, then once per REMIND_EVERY ticks after it.
should_alert=0
if [ "$fails" -eq "$FAIL_THRESHOLD" ]; then
  should_alert=1
elif [ "$fails" -gt "$FAIL_THRESHOLD" ] && [ $(( (fails - FAIL_THRESHOLD) % REMIND_EVERY )) -eq 0 ]; then
  should_alert=1
fi

if [ "$should_alert" = 0 ]; then
  if [ "$fails" -lt "$FAIL_THRESHOLD" ]; then
    echo "[watchdog] probe failed ($fails/$FAIL_THRESHOLD before alerting)"
  else
    echo "[watchdog] probe failed ($fails consecutive) — already alerted, next reminder in $(( REMIND_EVERY - (fails - FAIL_THRESHOLD) % REMIND_EVERY )) ticks"
  fi
  exit 0
fi

# Collect the context a responder needs first, so the alert is actionable
# without an SSH session. Every one of these is best-effort: the host is by
# definition in a bad state, and a failing diagnostic must not swallow the alert.
#
# `|| echo absent` is not enough on its own — a failing `docker inspect` still
# prints an empty line to stdout, which would put a blank line and then
# "absent" into the alert on two separate lines. Normalise empty to absent.
state_of() {
  local out
  out="$(docker inspect --format '{{.State.Status}}' "$1" 2>/dev/null)" || out=""
  printf '%s' "${out:-absent}"
}
api="$(state_of audr-api-1)"
db="$(state_of audr-db-1)"
ps="$(cd "${AUDR_REMOTE_DIR:-$HOME/audr}" 2>/dev/null && docker compose ps --format '{{.Service}}: {{.State}}' 2>/dev/null | head -20 || true)"

"$NOTIFY" "$(printf '%s\n' \
  "🔴 audr stand DOWN" \
  "" \
  "/health/ready has failed ${fails} consecutive probes (~${fails} min) as of ${stamp}." \
  "probe    : ${PROBE_URL}" \
  "response : ${ready:-<no response>}" \
  "api      : ${api}" \
  "db       : ${db}" \
  "" \
  "${ps:-<docker compose ps unavailable>}" \
  "" \
  "Runbook: docs/deploy-runbook.md — 'Stand down after a failed deploy'.")"

echo "[watchdog] ALERT sent — $fails consecutive failures"
exit 0
