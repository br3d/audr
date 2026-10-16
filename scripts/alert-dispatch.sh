#!/usr/bin/env bash
# Deliver pending critical news alerts from the stack to a real channel (ADR-007
# phase 2, AUD-584).
#
#   scripts/alert-dispatch.sh              one tick; what audr-alert-dispatch.timer runs
#   scripts/alert-dispatch.sh --dry-run    renders and reports, delivers nothing,
#                                          marks nothing — safe on a live stand
#
# WHY A HOST SCRIPT AND NOT CODE IN THE WORKER. `news_analyze` writes alerts
# inside the stack, but the thing that can deliver one — scripts/notify.sh — is a
# host script needing curl, python3 and secrets/, and the worker image has no
# curl by design (ADR-007 §1). So this is the bridge: the worker reports what is
# pending over `docker compose exec`, this sends it, and the worker records the
# outcome. No new auth surface — `docker compose exec` is a capability the deploy
# user already has — and no "endpoint for the host".
#
# ONE TICK = READ, SEND, MARK, per alert:
#   docker compose exec -T worker python -m audr.jobs.alerts_pending   -> JSON
#   scripts/notify.sh --key news-<asset>-<category> -- <text>          -> push
#   docker compose exec -T worker python -m audr.jobs.alerts_mark <id> -> dispatched_at
#
# Marking happens per alert, immediately after its notify.sh returns 0, rather
# than in one batch at the end: the window in which a crash re-sends an alert is
# then one alert wide instead of the whole tick. The remaining duplicate is
# accepted (ADR-007 §1, at-least-once) because --key collapses it into a comment
# on the board issue already open.
#
# NO --resolve, EVER. A news story has no "incident over" moment to detect, so
# nothing here can honestly close an alert issue; the wave of follow-up articles
# is collapsed by the 24h cooldown in the database plus --key. The board issue is
# closed by the assignee it woke (ADR-007 rev 2.1), which is also why this script
# never tries to.
#
# EXIT CODE. 0 on anything it is written to handle, including a stack that is
# down — that outage is audr-watchdog's alert to send, and a unit failure here
# would only add journal noise saying the same thing. Non-zero only when it
# cannot do its job at all (no notify.sh).
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Where the live stack's compose.yaml lives — `docker compose` is run from there,
# same convention as stand-watchdog.sh's diagnostics.
REMOTE_DIR="${AUDR_REMOTE_DIR:-$HOME/audr}"
WORKER_SERVICE="${AUDR_WORKER_SERVICE:-worker}"
NOTIFY="${AUDR_NOTIFY_BIN:-$REPO_DIR/scripts/notify.sh}"
# Bounded per tick for the same reason the worker side bounds it: a burst must
# not become fifty board issues in one minute.
LIMIT="${AUDR_ALERT_DISPATCH_LIMIT:-20}"
# A hung `docker compose exec` must not leave the unit running into the next
# tick. 60s is generous for a query behind a partial index.
EXEC_TIMEOUT="${AUDR_ALERT_DISPATCH_TIMEOUT:-60}"

dry=0
case "${1:-}" in
  --dry-run) dry=1 ;;
  "")        ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

[ -x "$NOTIFY" ] || { echo "[alert-dispatch] $NOTIFY missing or not executable" >&2; exit 1; }

# `timeout` is applied here rather than inside the loop so both calls share one
# definition of "too long".
worker_exec() {
  ( cd "$REMOTE_DIR" 2>/dev/null || exit 70
    timeout "$EXEC_TIMEOUT" docker compose exec -T "$WORKER_SERVICE" "$@" )
}

pending="$(worker_exec python -m audr.jobs.alerts_pending --limit "$LIMIT" 2>/dev/null)"
rc=$?
if [ "$rc" -ne 0 ]; then
  # Expected states, all of them someone else's alert to send: the stand is down
  # (audr-watchdog), the image predates these modules (the next deploy fixes it),
  # or the compose directory is wrong (install-time, visible on the first tick).
  echo "[alert-dispatch] cannot read pending alerts (exit $rc) — nothing dispatched"
  exit 0
fi

# Turn the JSON into one TSV line per alert, because a bash loop over JSON is how
# a quote or newline in an article title becomes a broken command line. python3
# also validates the contract: anything that is not the expected array makes this
# tick a no-op instead of a half-parsed send. Tabs and newlines inside fields are
# flattened — they are display text here, and the line framing must survive them.
if ! count_and_rows="$(AUDR_PENDING_JSON="$pending" python3 -c '
import json, os, sys

raw = os.environ["AUDR_PENDING_JSON"].strip()
if not raw:
    print(0)
    sys.exit(0)
try:
    alerts = json.loads(raw)
except ValueError as exc:
    print(f"not JSON: {exc}", file=sys.stderr)
    sys.exit(1)
if not isinstance(alerts, list):
    print("expected a JSON array", file=sys.stderr)
    sys.exit(1)

def flat(value: object) -> str:
    return " ".join(str(value or "").split())

print(len(alerts))
for alert in alerts:
    print("\t".join(flat(alert.get(field)) for field in (
        "id", "key", "asset_symbol", "category", "summary",
        "title", "url", "news_site", "published_at", "classifier",
    )))
' 2>&1)"; then
  echo "[alert-dispatch] alerts_pending did not return the expected JSON: $count_and_rows" >&2
  exit 0
fi

total="$(printf '%s\n' "$count_and_rows" | head -1)"
case "$total" in ''|*[!0-9]*) total=0 ;; esac

if [ "$total" = 0 ]; then
  # One line per quiet tick, on purpose: a dead timer and a quiet news day must
  # not look identical in `journalctl -u audr-alert-dispatch`. Same reasoning as
  # stand-watchdog.sh's "[watchdog] ok".
  echo "[alert-dispatch] no pending critical alerts"
  exit 0
fi

stamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
sent=0
failed=0

# Fed by redirection rather than a pipe so the counters survive the loop — a
# `while read` on the right-hand side of a pipe runs in a subshell.
while IFS=$'\t' read -r id key symbol category summary title url news_site published_at classifier; do
  [ -n "$id" ] || continue

  message="$(printf '%s\n' \
    "🔴 audr: ${category} alert on ${symbol}" \
    "" \
    "${summary}" \
    "" \
    "article  : ${title}" \
    "source   : ${news_site:-unknown}${published_at:+ (${published_at})}" \
    "link     : ${url:-<none>}" \
    "severity : critical (classifier: ${classifier:-heuristic})" \
    "dispatched by audr-alert-dispatch on $(hostname) at ${stamp}" \
    "" \
    "This is a portfolio asset of yours, so the question is whether the" \
    "position needs action before the market prices this in. The alert is" \
    "a heuristic match on a headline, NOT a verified fact — read the link" \
    "before acting on it." \
    "" \
    "Further articles about the same incident will comment on this issue" \
    "rather than open new ones (one alert per asset x category per 24h)." \
    "Close it yourself when you have dealt with it — nothing auto-closes it." \
    "" \
    "In-app: the alerts list carries every severity, including the warnings" \
    "that are deliberately not pushed here.")"

  notify_args=(--key "$key")
  [ "$dry" = 1 ] && notify_args+=(--dry-run)

  # `--` is the boundary between flags and the alert body: a headline is
  # untrusted text and may well start with a dash (ADR-007 §2, prompt-injection
  # note — the same reasoning applies to argument injection).
  notify_out="$("$NOTIFY" "${notify_args[@]}" -- "$message" 2>&1)"
  notify_rc=$?
  printf '%s\n' "$notify_out" | sed 's/^/[alert-dispatch]   /'

  if [ "$notify_rc" -eq 0 ]; then
    sent=$((sent + 1))
    if [ "$dry" = 1 ]; then
      echo "[alert-dispatch] DRY RUN — would mark $id dispatched"
      continue
    fi
    if ! mark_out="$(worker_exec python -m audr.jobs.alerts_mark "$id" 2>&1)"; then
      # Delivered but not marked: the next tick will send it again and --key
      # will collapse it. Say so rather than failing, so the journal explains
      # the duplicate somebody may see on the board.
      echo "[alert-dispatch] WARNING delivered $id but could not mark it: ${mark_out}" >&2
    fi
  else
    failed=$((failed + 1))
    echo "[alert-dispatch] notify.sh failed for $id (exit $notify_rc)" >&2
    [ "$dry" = 1 ] && continue
    # Recorded on the row, not retried here: the row stays pending, so the next
    # tick is the retry, and the UI can show why the last attempt failed.
    #
    # `--error=` rather than `--error `: notify.sh's last line is arbitrary text
    # and may begin with a dash, which argparse would read as another flag.
    worker_exec python -m audr.jobs.alerts_mark \
      "--error=notify.sh exit ${notify_rc}: $(printf '%s' "$notify_out" | tail -1)" "$id" \
      >/dev/null 2>&1 \
      || echo "[alert-dispatch] could not record dispatch_error for $id" >&2
  fi
done <<EOF
$(printf '%s\n' "$count_and_rows" | tail -n +2)
EOF

if [ "$dry" = 1 ]; then
  echo "[alert-dispatch] DRY RUN — $total pending, $sent would have been accepted, $failed would have failed"
else
  echo "[alert-dispatch] $total pending, $sent dispatched, $failed failed"
fi
exit 0
