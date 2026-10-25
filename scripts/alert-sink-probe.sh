#!/usr/bin/env bash
# Ask the deploy host's alert sinks whether their credentials still deliver, and
# raise one keyed, self-resolving alert when one is configured and refuses
# (AUD-757).
#
#   scripts/alert-sink-probe.sh              probe, and alert on a dead credential
#   scripts/alert-sink-probe.sh --dry-run    probe and print; deliver nothing
#
# Exits with install-watchdog.sh --check-sinks' code: 0 a sink answered, 1 a sink
# is configured and REFUSED, 3 nothing is configured, 2 the probe could not run.
# Callers that must not fail on a dead sink — the deploy workflow is one —
# swallow the status deliberately and say so.
#
# WHAT THIS IS FOR
# ----------------
# `notify-board.py --probe` has existed since AUD-594 precisely to answer "does
# this credential deliver?", and its only caller was install-watchdog.sh — i.e.
# install time, which is the one moment a freshly-pasted credential is certain
# to work. Nothing asked again.
#
# AUD-753 is the bill for that. The host's secrets/paperclip.env held a
# run-scoped agent JWT with a 48-hour TTL (which docs/deploy-runbook.md forbids,
# for exactly this reason). It died on 2026-10-16 and nobody noticed until
# 2026-10-25 — nine days in which the host looked configured and every alert
# bought a 401.
#
# Nothing was silenced: notify.sh spools what no sink took and
# scripts/drain-alert-spool.py replays it from an agent run. But that is the
# SLOW path by design (AUD-591), measured in heartbeats rather than in the probe
# window, and for nine days it was the only path. A dead credential on the alert
# channel is the quietest possible failure — it is not noticed by its own alert
# failing to arrive — so it has to be asked about on a schedule rather than
# remembered.
#
# WHY NOT FOLD IT INTO THE DRIFT STEP
# -----------------------------------
# `install-watchdog.sh --check-drift`, which the deploy workflow already runs
# every deploy via scripts/host-install-drift.sh, deliberately skips the sink
# probes: an expired Telegram token should not red a step that is about content
# drift. That reasoning is sound and is left alone. This is a separate step with
# its own key, so the two conditions keep their own board issues and their own
# first questions — "re-run the installer" and "mint a new credential" are
# different fixes with different owners.
#
# ONE ALERT PER CONDITION, NOT PER DEPLOY
# ---------------------------------------
# --key gives the board sink a condition to dedupe against: the first drifted
# deploy opens one `critical` issue and every later one comments on it instead
# of opening a second (scripts/notify.sh). --resolve closes it once a sink
# answers again, so replacing the credential clears the alert without anybody
# having to remember to. Without the key this would open an issue per deploy,
# which is how an alert channel gets muted.
#
# AUD-757 named this key `board-credential-dead`. It is `alert-sink-dead`
# because --check-sinks probes both sinks and this script alerts on either: a
# dead Telegram token filed under a board-shaped key would mislabel the fix.
#
# EXIT 3 IS QUIET
# ---------------
# "No credential at all" is the deploy host's current and intended state — only
# a human board user can mint the long-lived `task_bridge` key the board sink
# wants, and AUD-591 settled that the host runs on the spool until one exists.
# Alerting on it would fire on every deploy forever. It does not --resolve
# either: a credential that was dead and has since been DELETED has not
# recovered, and closing the issue would say it had.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NOTIFY="$ROOT/scripts/notify.sh"
PROBE="$ROOT/scripts/install-watchdog.sh"
ALERT_KEY=alert-sink-dead

DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  "")        ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

if [ ! -f "$PROBE" ]; then
  echo "==> $PROBE is missing; the alert sinks were NOT probed" >&2
  exit 2
fi

echo "==> install-watchdog.sh --check-sinks"
out="$(bash "$PROBE" --check-sinks 2>&1)"
rc=$?
printf '%s\n' "$out"
echo

# The verdict is read off install-watchdog.sh's marker line, not off the exit
# code alone. Exit 1 there also covers conditions that are not about a
# credential at all — `audr_require` fails with 1 when AUDR_DEPLOY_HOST is
# unset, and an ssh that cannot connect fails the same way — and on any machine
# that is not the deploy host that is the normal state. Reading either as "the
# credential is dead" opens a `critical` issue about a host nobody named, which
# is the mistake scripts/host-install-drift.sh made on its first run.
#
# A false alert costs more than a late one here: it trains the reader to ignore
# this key, and this key is the only thing standing between a silently dead
# alert channel and somebody noticing.
verdict=""
case "$out" in
  *"==> SINK VERDICT: dead"*)         verdict=dead ;;
  *"==> SINK VERDICT: live"*)         verdict=live ;;
  *"==> SINK VERDICT: unconfigured"*) verdict=unconfigured ;;
esac

if [ -z "$verdict" ]; then
  echo "==> COULD NOT PROBE (exit $rc): --check-sinks printed no verdict."
  echo "    Nothing is alerted and nothing is resolved: 'the probe did not run'"
  echo "    is a different fact from 'the sinks answer', and treating it as"
  echo "    either would be a lie — one that opens a critical issue about a host"
  echo "    nobody named, or that closes one that is still true."
  echo "    Set AUDR_DEPLOY_HOST (see deploy.env.example) and re-run."
  exit 2
fi

if [ "$verdict" = unconfigured ]; then
  echo "==> no sink is configured on this host — quiet by design (see the header)."
  exit 3
fi

if [ "$verdict" = live ]; then
  echo "==> an alert sink answered its probe"
  # Resolved unconditionally rather than only when an alert is known to be open:
  # this script has no memory between runs, and notify.sh treats a --resolve
  # with nothing open as a no-op. Skipped under --dry-run, which must deliver
  # nothing in either direction.
  if [ "$DRY" -eq 0 ] && [ -x "$NOTIFY" ]; then
    "$NOTIFY" --key "$ALERT_KEY" --resolve \
      "Alert sink recovered — a configured sink on the deploy host answers its probe again." \
      >/dev/null 2>&1 || true
  fi
  exit 0
fi

body="An alert sink on the deploy host is CONFIGURED and its credential is REFUSED.

Commit checked: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')

Why this matters: alerts from this host are not reaching anyone directly. They
are not lost — notify.sh spools every undelivered alert to
~/.local/state/audr-notify/undelivered.jsonl and scripts/drain-alert-spool.py
replays it from an agent run — but that is the SLOW path, measured in
heartbeats. Until this is fixed, nothing on this host pages anyone in minutes.

The usual cause is an expired key, and it looks exactly like this: the probe
reports HTTP 401. A run-scoped agent JWT has a 48-hour TTL and must never be
installed here (docs/deploy-runbook.md); the board sink wants a long-lived
\`task_bridge\` key, which only a human board user can mint. AUD-753 is the
precedent: nine days of exactly this state.

Fix: replace the credential in ~/audr/secrets/paperclip.env (or telegram.env),
then confirm with:
  scripts/install-watchdog.sh --check-sinks
This alert closes itself on the next deploy once a sink answers.

Full probe report:

$out"

if [ "$DRY" -eq 1 ]; then
  echo "==> --dry-run: would have alerted with key '$ALERT_KEY':"
  printf '%s\n' "$body"
  exit 1
fi

if [ -x "$NOTIFY" ]; then
  # notify.sh exits non-zero only when alerting ITSELF is broken (no sink
  # answered AND nothing spooled). Worth saying out loud here, because this
  # alert is about the alert path: the spool is the expected road for it, and
  # losing even that means the finding went nowhere.
  if ! printf '%s\n' "$body" | "$NOTIFY" --key "$ALERT_KEY"; then
    echo "==> the dead-sink alert reached no sink AND could not be spooled — this" >&2
    echo "    finding is now only in this run log." >&2
  fi
else
  echo "==> $NOTIFY is missing or not executable; the dead sink was NOT alerted" >&2
fi

exit 1
