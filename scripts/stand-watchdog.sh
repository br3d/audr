#!/usr/bin/env bash
# Probe the stand's /health/ready and alert on a sustained outage (AUD-444),
# plus four slower conditions that have no timer of their own: backup staleness
# (AUD-567), disk pressure (AUD-580), a mute alert dispatcher (AUD-592) and an
# alert spool nobody is draining (AUD-595).
#
# Runs on the deploy host from audr-watchdog.timer, once a minute. One tick =
# one probe; all the state between ticks is a counter in a file.
#
# WHY THE SECONDARY CHECKS LIVE HERE. Both watch a condition that would
# otherwise need a timer of its own — and that timer would then need its own
# liveness proof, recursively, which is exactly the trap AUD-567 fell into. This
# unit is observably firing every 60s in the journal, so hanging the checks off
# it is the one place that needs no new proof. Each is independent of the probe
# and of each other: they run under `|| true` and cannot change its outcome.
# The probe's own curl runs before all of them, so a slow check can delay an
# outage alert by its timeout but can never lose the reading.
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
# runs as an `if: always()` step inside build.yaml/deploy.yaml, so that output lands
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

# --- alert dispatch lag (AUD-592) --------------------------------------------
# scripts/alert-dispatch.sh is deliberately mute when it cannot read: on any
# failed `docker compose exec` it prints "cannot read pending alerts (exit N) —
# nothing dispatched" and exits 0, because every cause of that is someone else's
# alert to send (stand down -> this watchdog; stale image -> the next deploy;
# wrong compose dir -> install time). The cost of that choice is that a green
# unit with ExecMainStatus=0 is indistinguishable from a bridge that cannot
# deliver anything at all.
#
# That is not hypothetical. After AUD-584 phase 2 merged, the timer ticked every
# minute from 17:57 while the stand still ran the pre-merge image
# 0.1.2-g00957c0d303e, which has no `audr.jobs.alerts_pending` — four real runs,
# all "Deactivated successfully", zero possible deliveries. It was found only by
# reading the journal by eye. A sibling run hit the same end state by a second
# road the same heartbeat: with AUDR_REMOTE_DIR unexported, a genuine critical
# was *spooled* as `unconfigured` instead of delivered, and the spool only moves
# when an agent runs drain-alert-spool.py by hand. Both bugs are fixed; the
# failure class — no delivery, and nobody finds out — was not.
#
# audr-alert-dispatch.timer argues that a stopped dispatcher is not silent,
# because pending alerts stay visible in-app with dispatched_at IS NULL. That is
# true for a logged-in owner and false for exactly the audience phase 2 exists
# to reach: the one who is not looking at the app.
#
# WHY THIS READS THE DATABASE DIRECTLY instead of reusing alerts_pending. A
# check that went through the same `docker compose exec worker python -m
# audr.jobs.alerts_*` path would have been just as blind to the real AUD-584
# outage, whose cause was that module being absent from the running image. psql
# in the db container is a read path that shares no code with the dispatcher, so
# it still sees the rows when the dispatcher's own half is broken. Same reason
# this is a check on the already-proven watchdog rather than a fourth timer: a
# timer watching a timer needs its own liveness proof, recursively (AUD-567).
DISPATCH_DIR="${AUDR_REMOTE_DIR:-$HOME/audr}"
DISPATCH_DB_SERVICE="${AUDR_DB_SERVICE:-db}"
DISPATCH_DB_NAME="${AUDR_DB_NAME:-audr}"
DISPATCH_DB_USER="${AUDR_DB_USER:-audr}"
# 15 min against a 60s tick: fifteen chances to deliver before this calls it
# broken, so a single wedged exec, a deploy restarting the worker, or a tick
# that lands between the row's INSERT and its own query cannot fire it.
#
# The grace applies to the lag trigger ONLY. A recorded dispatch_error fires on
# the next tick with no grace at all, deliberately: alert-dispatch.sh records
# that column when notify.sh exits NON-ZERO, and notify.sh exits 0 for every
# delivery outcome it is written to handle — including "no sink accepted it, so
# it is spooled". A non-zero exit therefore means notify.sh could not run at
# all, which no amount of waiting improves.
#
# NOTE a limit this check inherits from that same contract, by design. Because
# notify.sh exits 0 on a spool, alert-dispatch.sh marks the row dispatched
# (`dispatched_via = 'spool'` since AUD-595) and it leaves the pending set — so
# the AUD-584 spool road ("delivered to nobody, and the spool only moves when an
# agent drains it") is NOT visible here, and must not be: making those rows stay
# pending would, against a 401 board token, keep every alert pending forever and
# make this check fire continuously. The pending set stays a faithful artefact
# of road one; road two has its own artefact and its own check below —
# check_alert_spool_age, on the age of the oldest line in undelivered.jsonl.
DISPATCH_LAG_MIN="${AUDR_DISPATCH_LAG_MINUTES:-15}"
# Hours, like the disk path: the condition is "delivery is broken", which is
# worth repeating while it lasts but not every minute.
DISPATCH_REMIND_H="${AUDR_DISPATCH_REMIND_HOURS:-6}"
DISPATCH_KEY="${AUDR_DISPATCH_ALERT_KEY:-alert-dispatch-stale}"
# A wedged exec must not hold this tick into the next one. The query is a count
# behind ix_news_alert_pending; 20s is generous.
DISPATCH_TIMEOUT="${AUDR_DISPATCH_QUERY_TIMEOUT:-20}"

# --- undrained alert spool (AUD-595) ----------------------------------------
# The road the dispatch-lag check above cannot see. notify.sh exits 0 when no
# sink accepted an alert — it parks it in undelivered.jsonl for
# scripts/drain-alert-spool.py to replay — so the row is marked dispatched
# (`dispatched_via = 'spool'` since AUD-595) and leaves the pending set. That is
# correct: spool->drain is an accepted delivery path, and holding the row
# pending instead would, against the stand's current 401 board token, keep every
# alert pending forever and make the lag check fire continuously.
#
# So the artefact for THIS road is the spool file itself, exactly as AUD-567's
# backup check watches the dump's mtime rather than the timer: a line that has
# sat here longer than the drain's cadence means nobody has been pushed at, and
# no amount of green units says otherwise.
#
# Oldest line, not newest: a fresh alert arriving on top of a week-old one must
# not reset the clock on the week-old one.
SPOOL_FILE_PATH="${AUDR_NOTIFY_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/audr-notify}/undelivered.jsonl"
# Both files, because a drain claims the spool by MOVING it aside: it appends to
# `undelivered.jsonl.draining`, replays from there, and removes it only once
# every record is delivered. An interrupted drain therefore leaves undelivered
# records in the claim file, where the next drain picks them up — and where this
# check would not see them at all if it only read the live spool. That is the
# same shape of blind spot as the one AUD-595 exists to close.
SPOOL_CLAIM_PATH="${SPOOL_FILE_PATH}.draining"
# 12h against a drain routine that runs every ~3h (routine 92db1531): four
# missed drains before this speaks, so a drain that is merely late, or an agent
# heartbeat that slipped, cannot fire it. It is still far inside the day-scale
# window in which an undelivered `critical` stops being worth delivering.
SPOOL_MAX_AGE_H="${AUDR_SPOOL_MAX_AGE_HOURS:-12}"
SPOOL_REMIND_H="${AUDR_SPOOL_REMIND_HOURS:-12}"
SPOOL_KEY="${AUDR_SPOOL_ALERT_KEY:-alert-spool-stale}"

mkdir -p "$STATE_DIR" || { echo "[watchdog] cannot create $STATE_DIR" >&2; exit 1; }

BACKUP_STATE_FILE="$STATE_DIR/backup-stale-alerted-at"
DISK_STATE_FILE="$STATE_DIR/disk-pressure-alerted-at"
DISPATCH_STATE_FILE="$STATE_DIR/alert-dispatch-alerted-at"
SPOOL_STATE_FILE="$STATE_DIR/alert-spool-stale-alerted-at"

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

# Same contract as the two checks above: runs before the probe's branches,
# cannot change its outcome, every failure path returns rather than exits.
check_alert_dispatch_lag() {
  local row rc stderr_file stale errored oldest sample now last headline detail field

  case "$DISPATCH_LAG_MIN" in
    ''|*[!0-9]*)
      echo "[watchdog] AUDR_DISPATCH_LAG_MINUTES is not a number — skipping dispatch check"
      return 0
      ;;
  esac

  now="$(date -u +%s)"
  stderr_file="$STATE_DIR/dispatch-query.err"

  # One statement, one exec, fed on stdin. The counts and the sample error come
  # back as a single tab-separated row:
  #   stale    pending criticals older than the threshold
  #   errored  pending criticals whose last delivery attempt was recorded failed
  #   oldest   age in whole minutes of the oldest pending critical
  #   sample   one of the recorded dispatch_error strings, flattened
  #
  # ON_ERROR_STOP makes psql exit non-zero on a SQL error, which it otherwise
  # does not — without it a missing table would come back as exit 0 with an
  # empty row, i.e. "nothing pending", which is the precise lie this check
  # exists to stop telling.
  #
  # STDIN, NOT -c, AND THAT IS LOAD-BEARING. psql interpolates `:'var'` in its
  # own lexer, and a -c string is handed to the server unparsed — so the same
  # query with -c fails with `syntax error at or near ":"`, every tick, which
  # this check would then report as "cannot read". Measured against
  # postgres:16-alpine, not inferred. Keeping the threshold a psql variable
  # rather than pasting it into the SQL is why it is worth the pipe.
  #
  # max(dispatch_error) is an arbitrary-but-deterministic pick among the failed
  # rows. It is a sample for the alert body, not a report: the full set is in
  # the table and the alert says how to read it. Tabs, newlines and carriage
  # returns are translated out of it because the row framing must survive an
  # error string that contains them.
  row="$(printf '%s\n' "
          select count(*) filter (where created_at < now() - (:'lag' || ' minutes')::interval),
                 count(*) filter (where dispatch_error is not null),
                 coalesce(max(floor(extract(epoch from now() - created_at) / 60))::bigint, 0),
                 coalesce(left(translate(max(dispatch_error), chr(10) || chr(9) || chr(13), '   '), 200), '')
          from news_alert
          where dispatched_at is null and severity = 'critical'
        " | ( cd "$DISPATCH_DIR" 2>/dev/null || exit 70
      timeout "$DISPATCH_TIMEOUT" docker compose exec -T "$DISPATCH_DB_SERVICE" \
        psql -U "$DISPATCH_DB_USER" -d "$DISPATCH_DB_NAME" \
        -v ON_ERROR_STOP=1 -v lag="$DISPATCH_LAG_MIN" -tA -F $'\t' ) 2>"$stderr_file" )"
  rc=$?

  if [ "$rc" -ne 0 ]; then
    # Unreadable is not evidence, in either direction — the same rule the disk
    # check follows. Every cause already has an owner: a down stand is this
    # script's own outage alert a few lines below, and a database without
    # migration 0033 is a stand where phase 1 has not deployed yet. Saying which
    # one it was costs one journal line and saves an SSH session.
    if grep -qi 'does not exist' "$stderr_file" 2>/dev/null; then
      echo "[watchdog] news_alert does not exist yet (migration 0033 not applied) — dispatch check skipped"
    else
      echo "[watchdog] cannot read news_alert (exit $rc) — dispatch check skipped"
    fi
    return 0
  fi

  IFS=$'\t' read -r stale errored oldest sample <<<"$row"
  for field in stale errored oldest; do
    case "${!field}" in
      ''|*[!0-9]*)
        echo "[watchdog] unexpected row from news_alert query ($field) — dispatch check skipped"
        return 0
        ;;
    esac
  done

  # Two triggers, one key, on purpose. A row with a dispatch_error is a
  # dispatcher that tried and was refused by notify.sh; a row past the threshold
  # is one that is not delivering at all. They want the same first action — go
  # look at why delivery is failing — and an errored row is almost always also
  # a stale one, so two keys would mean two board issues for one condition.
  # Which trigger fired is in the body instead.
  if [ "$stale" = 0 ] && [ "$errored" = 0 ]; then
    if [ -f "$DISPATCH_STATE_FILE" ]; then
      "$NOTIFY" --key "$DISPATCH_KEY" --resolve -- "$(printf '%s\n' \
        "✅ audr news alert dispatch is flowing again" \
        "" \
        "No critical news_alert row is pending past ${DISPATCH_LAG_MIN}m and none" \
        "carries a dispatch_error, as of ${stamp}." \
        "No action needed.")"
      rm -f "$DISPATCH_STATE_FILE"
      echo "[watchdog] alert dispatch recovered — stand-down sent"
    else
      # Said out loud for the same reason as "[watchdog] ok": a quiet news day
      # and a check that silently stopped running must not look identical.
      echo "[watchdog] alert dispatch ok (no critical alert pending over ${DISPATCH_LAG_MIN}m)"
    fi
    return 0
  fi

  if [ "$errored" != 0 ]; then
    headline="🟠 audr news alerts are FAILING to dispatch"
    detail="${errored} pending critical alert(s) have a recorded dispatch_error — notify.sh refused the delivery."
  else
    headline="🟠 audr news alerts are NOT being dispatched"
    detail="${stale} critical alert(s) have been pending for more than ${DISPATCH_LAG_MIN}m (oldest: ${oldest}m) with no dispatch_error recorded — nothing is even attempting delivery."
  fi

  last=0
  [ -f "$DISPATCH_STATE_FILE" ] && last="$(cat "$DISPATCH_STATE_FILE" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ "$last" -ne 0 ] && [ $(( now - last )) -lt $(( DISPATCH_REMIND_H * 3600 )) ]; then
    echo "[watchdog] alert dispatch stale (stale=${stale} errored=${errored}) — already alerted, reminding in $(( (DISPATCH_REMIND_H * 3600 - (now - last)) / 3600 ))h"
    return 0
  fi

  "$NOTIFY" --key "$DISPATCH_KEY" -- "$(printf '%s\n' \
    "$headline" \
    "" \
    "$detail" \
    "Checked at ${stamp} by audr-watchdog on $(hostname)." \
    "" \
    "${sample:+last recorded error: ${sample}}" \
    "" \
    "The dispatcher exits 0 on an unreadable stack by design, so its unit is" \
    "green either way — do not read audr-alert-dispatch.service's status as an" \
    "answer. Read what it actually said:" \
    "  journalctl -u audr-alert-dispatch -n 30 --no-pager" \
    "" \
    "'cannot read pending alerts' means the worker half is unreachable; the" \
    "usual cause is an image that predates audr.jobs.alerts_pending, which the" \
    "next deploy fixes:" \
    "  cd ${DISPATCH_DIR} && docker compose exec -T worker python -m audr.jobs.alerts_pending" \
    "'no channel configured' means the alert was spooled, not delivered, and" \
    "the spool only moves when someone drains it." \
    "" \
    "Runbook: docs/deploy-runbook.md — 'News alert dispatch'.")"
  echo "$now" > "$DISPATCH_STATE_FILE"
  echo "[watchdog] ALERT sent — alert dispatch stale (stale=${stale} errored=${errored}, limit ${DISPATCH_LAG_MIN}m)"
}

# Same contract as the other three: runs before the probe, cannot change its
# outcome, every failure path returns rather than exits.
check_alert_spool_age() {
  local report oldest_h count now last

  if [ ! -f "$SPOOL_FILE_PATH" ] && [ ! -f "$SPOOL_CLAIM_PATH" ]; then
    # Nothing has ever needed spooling, or the drain cleared both files. Both
    # are the healthy state — said out loud anyway, like the other three
    # checks, so that a drained spool and a check that stopped running differ
    # in the journal.
    echo "[watchdog] alert spool ok (no spool file at ${SPOOL_FILE_PATH})"
    [ -f "$SPOOL_STATE_FILE" ] && {
      "$NOTIFY" --key "$SPOOL_KEY" --resolve -- "$(printf '%s\n' \
        "✅ audr alert spool is drained" \
        "" \
        "${SPOOL_FILE_PATH} is gone as of ${stamp} — every spooled alert has" \
        "been replayed." \
        "No action needed.")"
      rm -f "$SPOOL_STATE_FILE"
      echo "[watchdog] alert spool drained — stand-down sent"
    }
    return 0
  fi
  now="$(date -u +%s)"

  command -v python3 >/dev/null 2>&1 || {
    echo "[watchdog] no python3 — alert spool check skipped"
    return 0
  }

  # python3 reads the records rather than awk/jq: the file is JSONL whose
  # `message` field carries newlines, quotes and emoji, and nothing here may
  # depend on jq being installed (notify.sh already requires python3, so this
  # adds no dependency).
  #
  # Records under this check's OWN key are excluded from the age. The alert
  # below is itself sent through notify.sh, which on a stand with no working
  # sink spools it — so counting it would make the condition permanently true
  # from its own first message and the file could never read as healthy again.
  report="$(AUDR_SPOOL_PATH="$SPOOL_FILE_PATH" AUDR_SPOOL_CLAIM_PATH="$SPOOL_CLAIM_PATH" \
    AUDR_SPOOL_SELF_KEY="$SPOOL_KEY" python3 -c '
import json, os, sys, time

paths = [os.environ["AUDR_SPOOL_PATH"], os.environ["AUDR_SPOOL_CLAIM_PATH"]]
self_key = os.environ.get("AUDR_SPOOL_SELF_KEY", "")
now = time.time()
oldest = None
count = 0
for path in paths:
    try:
        fh = open(path, encoding="utf-8")
    except FileNotFoundError:
        # Either file may be absent: the claim file exists only mid-drain or
        # after an interrupted one, and the live spool only once something has
        # been spooled.
        continue
    except OSError as exc:
        print(f"unreadable: {exc}", file=sys.stderr)
        sys.exit(1)
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # A truncated last line (a tick that read the file mid-append)
                # is not evidence of anything; the next tick sees it whole.
                continue
            if record.get("key", "") == self_key:
                continue
            count += 1
            stamp = str(record.get("ts", ""))
            try:
                seconds = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
            except ValueError:
                # No usable timestamp. Treated as old rather than ignored: a
                # record nobody can date has still not been delivered.
                seconds = 0.0
            age = max(0.0, now - seconds)
            oldest = age if oldest is None else max(oldest, age)
print(f"{count} {int((oldest or 0) // 3600)}")
' 2>/dev/null)" || {
    echo "[watchdog] cannot read ${SPOOL_FILE_PATH} — alert spool check skipped"
    return 0
  }

  count="${report%% *}"
  oldest_h="${report##* }"
  for field in count oldest_h; do
    case "${!field}" in
      ''|*[!0-9]*)
        echo "[watchdog] unexpected spool reading ($field) — alert spool check skipped"
        return 0
        ;;
    esac
  done

  if [ "$count" = 0 ] || [ "$oldest_h" -lt "$SPOOL_MAX_AGE_H" ]; then
    if [ -f "$SPOOL_STATE_FILE" ]; then
      "$NOTIFY" --key "$SPOOL_KEY" --resolve -- "$(printf '%s\n' \
        "✅ audr alert spool is flowing again" \
        "" \
        "${count} spooled alert(s) in ${SPOOL_FILE_PATH}, oldest ${oldest_h}h" \
        "(limit ${SPOOL_MAX_AGE_H}h), as of ${stamp}." \
        "No action needed.")"
      rm -f "$SPOOL_STATE_FILE"
      echo "[watchdog] alert spool fresh (${count} record(s), oldest ${oldest_h}h) — stand-down sent"
    else
      # Said out loud for the same reason as "[watchdog] ok": a drained spool
      # and a check that silently stopped running must not look identical.
      echo "[watchdog] alert spool ok (${count} record(s), oldest ${oldest_h}h, limit ${SPOOL_MAX_AGE_H}h)"
    fi
    return 0
  fi

  last=0
  [ -f "$SPOOL_STATE_FILE" ] && last="$(cat "$SPOOL_STATE_FILE" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ "$last" -ne 0 ] && [ $(( now - last )) -lt $(( SPOOL_REMIND_H * 3600 )) ]; then
    echo "[watchdog] alert spool stale (${count} record(s), oldest ${oldest_h}h) — already alerted, reminding in $(( (SPOOL_REMIND_H * 3600 - (now - last)) / 3600 ))h"
    return 0
  fi

  "$NOTIFY" --key "$SPOOL_KEY" -- "$(printf '%s\n' \
    "🟠 audr alerts are PARKED ON DISK, not delivered" \
    "" \
    "${count} alert(s) are sitting in the spool on $(hostname), the oldest" \
    "${oldest_h}h old (limit ${SPOOL_MAX_AGE_H}h), as of ${stamp}." \
    "" \
    "These were ACCEPTED by notify.sh and recorded as dispatched — news" \
    "alerts carry dispatched_via = 'spool' — but no sink took them, so" \
    "nobody has been pushed at. Delivery happens when an agent replays the" \
    "spool, and that has not happened in ${oldest_h}h:" \
    "  python3 scripts/drain-alert-spool.py --dry-run   # what would be sent" \
    "  python3 scripts/drain-alert-spool.py             # send it, clear it" \
    "" \
    "The usual cause is the board credential: no secrets/paperclip.env, or a" \
    "token the API now rejects. Check which, from the host:" \
    "  cd ${DISPATCH_DIR} && scripts/notify.sh --dry-run --key spool-probe -- probe" \
    "A line reading 'credential REJECTED ... (HTTP 401)' is the answer; the" \
    "fix is a fresh long-lived task_bridge key, which only a board user can" \
    "mint (docs/deploy-runbook.md — 'Alerts the host could not send')." \
    "" \
    "This message took the same road it is reporting on, so you are most" \
    "likely reading it as a replay from that very drain." \
    "" \
    "Runbook: docs/deploy-runbook.md — 'When delivery only reaches the spool'.")"
  echo "$now" > "$SPOOL_STATE_FILE"
  echo "[watchdog] ALERT sent — alert spool stale (${count} record(s), oldest ${oldest_h}h, limit ${SPOOL_MAX_AGE_H}h)"
}

fails=0
[ -f "$STATE_FILE" ] && fails="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
case "$fails" in ''|*[!0-9]*) fails=0 ;; esac

ready="$(curl -s -m 10 "$PROBE_URL" 2>/dev/null || true)"
stamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

# Before the probe's own branches, which all exit. `|| true` so a bug in any
# secondary check cannot cost us the outage alert.
check_backup_age || true
check_disk_space || true
check_alert_dispatch_lag || true
check_alert_spool_age || true

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
