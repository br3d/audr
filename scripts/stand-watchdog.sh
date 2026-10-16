#!/usr/bin/env bash
# Probe the stand's /health/ready and alert on a sustained outage (AUD-444),
# plus two slower conditions that have no timer of their own: backup staleness
# (AUD-567) and disk pressure (AUD-580).
#
# Runs on the deploy host from audr-watchdog.timer, once a minute. One tick =
# one probe; all the state between ticks is a counter in a file.
#
# WHY THE SECONDARY CHECKS LIVE HERE. Both watch a condition that would
# otherwise need a timer of its own — and that timer would then need its own
# liveness proof, recursively, which is exactly the trap AUD-567 fell into. This
# unit is observably firing every 60s in the journal, so hanging the checks off
# it is the one place that needs no new proof. Each is independent of the probe
# and of the other: they run under `|| true` and cannot change its outcome.
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
# Names the condition for notify.sh's dedupe. Distinct from the deploy
# workflow's key so a failed deploy and a spontaneous outage stay separate
# threads — they have different first questions.
ALERT_KEY="${AUDR_ALERT_KEY:-stand-down}"

# --- backup staleness (AUD-567) ---------------------------------------------
# scripts/backup-run.sh alerts loudly when a backup FAILS, but a backup that
# never starts is silent: if audr-backup.timer stops firing — disabled by a
# daemon-reload, a mistyped OnCalendar, the unit file lost to a reinstall — then
# nothing runs, so nothing can complain. That is the exact shape of AUD-562,
# where the backup tooling was shipped and proven and then nothing ran it for
# two weeks, and of AUD-505, where a timer was authorised and closed while the
# unit did not exist. The absence of an alert is not evidence of a backup.
#
# Checking the dump's own mtime makes the artefact, not the schedule, the thing
# being watched: it stays true regardless of how the dump gets taken (timer,
# hand-run, a future replacement) and it fails loudly when nobody takes one.
# It lives in this script rather than in a second timer of its own because that
# timer would need its own liveness proof, recursively — whereas this one is
# observably firing every 60s in the journal.
BACKUP_DIR="${AUDR_BACKUP_DIR:-${AUDR_REMOTE_DIR:-$HOME/audr}/backups}"
# 26h, not 24h: the daily fire is 03:20 UTC with RandomizedDelaySec=10m, so two
# consecutive dumps can legitimately sit 24h20m apart, and a `Persistent=true`
# catch-up after a reboot can land later still. 26h clears all of that and
# still surfaces a missed 03:20 by ~05:30 the same morning.
BACKUP_MAX_AGE_H="${AUDR_BACKUP_MAX_AGE_HOURS:-26}"
# Same reasoning as REMIND_EVERY above, on a slower clock: a stale backup is a
# days-scale condition, so one message a day is the useful cadence.
BACKUP_REMIND_H="${AUDR_BACKUP_REMIND_HOURS:-24}"
BACKUP_KEY="${AUDR_BACKUP_ALERT_KEY:-backup-stale}"

# --- disk pressure (AUD-580) -------------------------------------------------
# scripts/host-gc.sh already measures this and already has two escalating WARN
# paths for it — "root filesystem still at N% after GC" and the much worse
# "Docker is no longer the thing filling this disk". Both only `echo`. host-gc
# runs as an `if: always()` step inside build.yaml/ci.yaml, so that output lands
# in a Gitea job log, and Act Runner's journal carries only its own meta lines:
# `journalctl -u act_runner | grep host-gc` returns nothing even for runs that
# did prune. Nobody reads a job log on a green deploy, so the most serious
# condition host-gc can detect was structurally invisible.
#
# The stakes are host-gc's own: "Disk exhaustion on this host does not fail
# politely: it takes the live stack's database volume down with it."
#
# This adds the missing voice, NOT a second GC lever — host-gc's AUD-425 header
# warns specifically against re-tuning retention, and on 2026-10-16 the windows
# measured correct: / moved 63% -> 75% in a day purely on merge rate (7 deploys
# in 11h, each leaving ~2x278MB of non-shared npm/uv exec-mount cache), with the
# image side provably pinned at its IMAGE_KEEP_COUNT=5 floor.
DISK_PATH="${AUDR_DISK_PATH:-/}"
# 85, deliberately ABOVE host-gc's WARN_PCT=80. 80% is the level at which
# host-gc's escalated pass is *designed* to run and reclaim, so alerting there
# would fire on every busy merge day and train the channel to be ignored. 85%
# means escalation has already run and still not recovered — host-gc's
# "investigate non-Docker growth" state, which is the actionable one.
DISK_WARN_PCT="${AUDR_DISK_WARN_PCT:-85}"
# Hours-scale condition: between the outage path's 1h and the backup path's 24h.
DISK_REMIND_H="${AUDR_DISK_REMIND_HOURS:-6}"
DISK_KEY="${AUDR_DISK_ALERT_KEY:-disk-pressure}"

mkdir -p "$STATE_DIR" || { echo "[watchdog] cannot create $STATE_DIR" >&2; exit 1; }

BACKUP_STATE_FILE="$STATE_DIR/backup-stale-alerted-at"
DISK_STATE_FILE="$STATE_DIR/disk-pressure-alerted-at"

# Runs before the health probe and deliberately cannot change its outcome: a
# stale backup and a down stand are independent conditions, and the probe is the
# more urgent of the two. Every failure path here returns rather than exits.
check_backup_age() {
  local newest age_h age_label now last detail
  now="$(date -u +%s)"

  # mtime of the most recent dump of any shape — .sql.age (pre-AUD-564) and
  # .sql.gz.age (current) both count, because either one is a real backup.
  newest="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'audr-*.sql*' -printf '%T@ %p\n' \
    2>/dev/null | sort -rn | head -1)"

  if [ -n "$newest" ]; then
    age_h=$(( (now - ${newest%%.*}) / 3600 ))
    if [ "$age_h" -lt "$BACKUP_MAX_AGE_H" ]; then
      # Stand down only if we had alerted — same rule as the probe's recovery
      # path, for the same reason.
      if [ -f "$BACKUP_STATE_FILE" ]; then
        "$NOTIFY" --key "$BACKUP_KEY" --resolve -- "$(printf '%s\n' \
          "✅ audr stand backups are current again" \
          "" \
          "Newest dump: ${newest#* } (${age_h}h old) as of ${stamp}." \
          "No action needed.")"
        rm -f "$BACKUP_STATE_FILE"
        echo "[watchdog] backup freshness recovered (${age_h}h) — stand-down sent"
      else
        echo "[watchdog] newest backup is ${age_h}h old (limit ${BACKUP_MAX_AGE_H}h)"
      fi
      return 0
    fi
    age_label="${age_h}h"
    detail="Newest dump is ${newest#* }, ${age_h}h old — older than the ${BACKUP_MAX_AGE_H}h limit."
  else
    # No dump at all is strictly worse than a stale one, and is what an
    # install that silently never ran looks like. Said explicitly, because
    # "0h old" would read like a passing check.
    age_label="no dump"
    detail="No audr-*.sql* dump exists in ${BACKUP_DIR} at all."
  fi

  last=0
  [ -f "$BACKUP_STATE_FILE" ] && last="$(cat "$BACKUP_STATE_FILE" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ "$last" -ne 0 ] && [ $(( now - last )) -lt $(( BACKUP_REMIND_H * 3600 )) ]; then
    echo "[watchdog] backup stale (${age_label}) — already alerted, reminding in $(( (BACKUP_REMIND_H * 3600 - (now - last)) / 3600 ))h"
    return 0
  fi

  "$NOTIFY" --key "$BACKUP_KEY" -- "$(printf '%s\n' \
    "🟠 audr stand backups are STALE" \
    "" \
    "$detail" \
    "Checked at ${stamp} by audr-watchdog on $(hostname)." \
    "" \
    "The db_data volume is the only copy of the data until a dump lands." \
    "First question is whether the schedule is still armed:" \
    "  systemctl list-timers audr-backup.timer --no-pager" \
    "  systemctl status audr-backup.service --no-pager -n 20" \
    "A disabled or missing unit is re-installed with scripts/install-backup.sh." \
    "" \
    "Runbook: docs/deploy-runbook.md — 'Backups'.")"
  echo "$now" > "$BACKUP_STATE_FILE"
  echo "[watchdog] ALERT sent — backups stale (${age_label}, limit ${BACKUP_MAX_AGE_H}h)"
}

# Same contract as check_backup_age: runs before the probe, cannot change its
# outcome, every failure path returns rather than exits.
check_disk_space() {
  local pct line now last dsdf
  now="$(date -u +%s)"

  # Field 5 of `df -P` is the use percentage. -P pins POSIX output so a long
  # device name cannot wrap the row and shift the column.
  pct="$(df -P "$DISK_PATH" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  # A non-numeric reading means df failed or the path is gone. Say so and stay
  # quiet rather than treating an unreadable disk as either full or healthy —
  # guessing in either direction is worse than one honest journal line.
  case "${pct:-}" in
    ''|*[!0-9]*)
      echo "[watchdog] cannot read disk usage for ${DISK_PATH}"
      return 0
      ;;
  esac
  line="$(df -h "$DISK_PATH" 2>/dev/null | awk 'NR==2 {print $3" used, "$4" free ("$5" of "$2")"}')"

  if [ "$pct" -lt "$DISK_WARN_PCT" ]; then
    if [ -f "$DISK_STATE_FILE" ]; then
      "$NOTIFY" --key "$DISK_KEY" --resolve -- "$(printf '%s\n' \
        "✅ audr stand disk pressure cleared" \
        "" \
        "${DISK_PATH} is back to ${pct}% (threshold ${DISK_WARN_PCT}%) as of ${stamp}." \
        "${line}" \
        "No action needed.")"
      rm -f "$DISK_STATE_FILE"
      echo "[watchdog] disk pressure cleared (${pct}%) — stand-down sent"
    else
      echo "[watchdog] disk at ${pct}% (limit ${DISK_WARN_PCT}%)"
    fi
    return 0
  fi

  last=0
  [ -f "$DISK_STATE_FILE" ] && last="$(cat "$DISK_STATE_FILE" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ "$last" -ne 0 ] && [ $(( now - last )) -lt $(( DISK_REMIND_H * 3600 )) ]; then
    echo "[watchdog] disk at ${pct}% — already alerted, reminding in $(( (DISK_REMIND_H * 3600 - (now - last)) / 3600 ))h"
    return 0
  fi

  # Carried in the alert so the first triage question — "is this Docker or
  # something else?" — is answered without an SSH session, matching the outage
  # alert's philosophy. Best-effort: a failing diagnostic must not swallow it.
  dsdf="$(docker system df 2>/dev/null || true)"

  "$NOTIFY" --key "$DISK_KEY" -- "$(printf '%s\n' \
    "🟠 audr stand disk is FILLING UP" \
    "" \
    "${DISK_PATH} is at ${pct}%, at or above the ${DISK_WARN_PCT}% threshold, as of ${stamp}." \
    "${line}" \
    "Checked by audr-watchdog on $(hostname)." \
    "" \
    "This is past the point where scripts/host-gc.sh escalates on its own" \
    "(it tightens to 6h windows at 80%), so GC has most likely already run" \
    "and not recovered the space. Disk exhaustion here takes the db_data" \
    "volume down with it, so this is worth looking at now." \
    "" \
    "${dsdf:-<docker system df unavailable>}" \
    "" \
    "First question is whether Docker is still the thing growing:" \
    "  scripts/host-gc.sh --dry-run    # what is resident, changes nothing" \
    "  sudo du -xh --max-depth=1 / | sort -rh | head" \
    "A large non-Docker directory means retention is not the problem." \
    "" \
    "Runbook: docs/deploy-runbook.md — 'Disk pressure'.")"
  echo "$now" > "$DISK_STATE_FILE"
  echo "[watchdog] ALERT sent — disk at ${pct}% (limit ${DISK_WARN_PCT}%)"
}

fails=0
[ -f "$STATE_FILE" ] && fails="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
case "$fails" in ''|*[!0-9]*) fails=0 ;; esac

ready="$(curl -s -m 10 "$PROBE_URL" 2>/dev/null || true)"
stamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

# Before the probe's own branches, which all exit. `|| true` so a bug in either
# secondary check cannot cost us the outage alert.
check_backup_age || true
check_disk_space || true

case "$ready" in
  *'"status":"ok"'*)
    # Recovery is only worth a message if we actually alerted. Crossing the
    # threshold is what put someone on the hook; telling them they can stand
    # down is the other half of that, and without it the only way to know is to
    # go and look — the exact habit this whole ticket is trying to remove.
    if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
      # --resolve closes the board issue this outage opened, so a recovered
      # stand does not leave a `critical` issue behind for someone to triage
      # and discover is already fine.
      "$NOTIFY" --key "$ALERT_KEY" --resolve -- "$(printf '%s\n' \
        "✅ audr stand RECOVERED" \
        "" \
        "/health/ready is returning ok again at ${stamp}." \
        "Outage spanned at least ${fails} consecutive probes (~${fails} min)." \
        "No action needed.")"
      echo "[watchdog] recovered after $fails consecutive failures — recovery alert sent"
    else
      # One line per healthy tick, so `journalctl -u audr-watchdog` answers
      # "is the watchdog actually running?" without anything to correlate.
      # A silent success path makes a dead timer and a healthy stand look
      # identical in the journal, which is the wrong failure mode for the
      # thing whose whole job is noticing.
      echo "[watchdog] ok"
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

# --key ties every message about this outage to one board issue: the first
# crossing opens it, the hourly reminders comment on it, and recovery above
# closes it. Without that the reminder cadence would mean a new `critical`
# issue every hour the stand stays down.
"$NOTIFY" --key "$ALERT_KEY" -- "$(printf '%s\n' \
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
