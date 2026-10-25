#!/usr/bin/env bash
# Ask every host installer whether the deploy host still runs this checkout's
# code, and alert when it does not (AUD-698).
#
#   scripts/host-install-drift.sh              check, and alert on drift
#   scripts/host-install-drift.sh --dry-run    check and print; deliver nothing
#
# Exits 0 when every pinned copy matches, 1 on drift, 2 when it could not tell
# (no host configured, SSH refused). Callers that must not fail on drift — the
# deploy workflow is one — swallow the status deliberately and say so.
#
# WHAT THIS IS FOR
# ----------------
# install-backup.sh, install-watchdog.sh and install-mirror-dropin.sh install
# PINNED COPIES of repo files to the host. That is the right design: the host's
# only git checkout belongs to the CI runner, which rewrites it on every deploy
# and resets it on a failed one, so a backup or watchdog living in it would stop
# working at the moment a bad deploy makes it matter most.
#
# The price is that a repo edit reaches the host only when the installer runs
# again — docs/operations.md has always said so — and until this script there
# was nothing that NOTICED the gap. Each installer's `--check` reported presence
# and unit status, never content, and exited 0 either way.
#
# That silence is the expensive part, not the drift. The drift class is the one
# that caused AUD-443: the host's copies of the CI workflow files fell behind
# the repo's, and the failure was invisible for two hours. Workflows ship with
# the commit now; these files deliberately cannot, so they are measured instead.
#
# And the things that drift here are the things that only run when something has
# already gone wrong — the backup, the watchdog, the alert path itself. A stale
# notify.sh is not noticed by its own alert failing to arrive; that is precisely
# what nobody sees. So the check has to be scheduled rather than remembered,
# which is why it runs from the deploy workflow on every deploy.
#
# ONE ALERT PER CONDITION, NOT PER RUN
# ------------------------------------
# --key gives the board sink a condition to dedupe against: the first drifted
# deploy opens one `critical` issue and every later one comments on it instead
# of opening a second (scripts/notify.sh). --resolve closes it once the host
# matches again, so a re-run of the installer clears the alert without anybody
# having to remember to. Without the key this would open an issue per deploy,
# which is how an alert channel gets muted.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NOTIFY="$ROOT/scripts/notify.sh"
ALERT_KEY=host-install-drift

DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  "")        ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

# Each installer owns its own manifest and its own notion of where it installed
# things, so the check is delegated rather than reimplemented here — this script
# only aggregates and alerts. `--check-drift` is the cheap mode: one SSH round
# trip, no sink probes, non-zero on drift.
INSTALLERS=(
  install-backup.sh
  install-watchdog.sh
  install-mirror-dropin.sh
)

report=""
drifted=()
unreachable=()

for installer in "${INSTALLERS[@]}"; do
  echo "==> $installer --check-drift"
  out="$("$ROOT/scripts/$installer" --check-drift 2>&1)"
  rc=$?
  printf '%s\n' "$out"
  report+="--- $installer (exit $rc)"$'\n'"$out"$'\n\n'

  # Drift is claimed on the REPORT, not on the exit code alone.
  #
  # Exit 1 is not specific enough: `audr_require` fails with 1 when
  # AUDR_DEPLOY_HOST is unset, which on any machine that is not the deploy host
  # is the normal state, and an ssh that cannot connect fails the same way.
  # Reading either as "the host is stale" opens a `critical` issue about a host
  # nobody named — which is exactly what the first run of this script did.
  #
  # A false alert here costs more than a missed one: it trains the reader to
  # ignore the key, and this key is the only thing standing between a silently
  # stale backup and somebody noticing. So drift requires audr_drift_report's
  # own marker, which only it can print and only after it has really compared
  # hashes on both ends.
  if [ "$rc" -eq 0 ]; then
    :
  elif printf '%s' "$out" | grep -q '=> DRIFT:'; then
    drifted+=("$installer")
  else
    unreachable+=("$installer (exit $rc)")
  fi
done

echo

if [ "${#unreachable[@]}" -gt 0 ] && [ "${#drifted[@]}" -eq 0 ]; then
  echo "==> COULD NOT CHECK: ${unreachable[*]}"
  echo "    Nothing is alerted, and nothing is resolved: this is 'the check did"
  echo "    not run', which is a different fact from 'the host matches'. Treating"
  echo "    it as either would be a lie — one that opens a critical issue about a"
  echo "    host nobody named, or that closes one that is still true."
  echo "    Set AUDR_DEPLOY_HOST (see deploy.env.example) and re-run."
  exit 2
fi

if [ "${#drifted[@]}" -eq 0 ]; then
  echo "==> every pinned copy on the host matches this checkout"
  # Resolve unconditionally rather than only when an alert is known to be open:
  # this script has no memory between runs, and notify.sh treats a --resolve
  # with no open issue as a no-op. Skipped on --dry-run, which must deliver
  # nothing in either direction.
  if [ "$DRY" -eq 0 ] && [ -x "$NOTIFY" ]; then
    "$NOTIFY" --key "$ALERT_KEY" --resolve \
      "Host install drift cleared — every pinned copy on the deploy host matches the repo again." \
      >/dev/null 2>&1 || true
  fi
  exit 0
fi

echo "==> DRIFT in: ${drifted[*]}"
[ "${#unreachable[@]}" -gt 0 ] && echo "==> also could not check: ${unreachable[*]}"

body="The deploy host is running PINNED COPIES that no longer match the repo.

Drifted installers: ${drifted[*]}
Commit checked:     $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')

Why this matters: these files are the backup, the watchdog and the alert path
itself. They only run when something has already gone wrong, so a stale copy is
not noticed by its own failure — which is why this is alerted rather than logged.

Fix: re-run the drifted installer(s) from a checkout of this commit, e.g.
  scripts/install-watchdog.sh
Then confirm with:
  scripts/host-install-drift.sh

Full report:

$report"

if [ "$DRY" -eq 1 ]; then
  echo
  echo "==> --dry-run: would have alerted with key '$ALERT_KEY':"
  printf '%s\n' "$body"
  exit 1
fi

if [ -x "$NOTIFY" ]; then
  # notify.sh exits non-zero only when alerting ITSELF is broken (no sink
  # answered and nothing spooled), which is worth saying out loud here: it means
  # the drift went unreported, and the drift may well be in notify.sh.
  if ! printf '%s\n' "$body" | "$NOTIFY" --key "$ALERT_KEY"; then
    echo "==> the drift alert could not be delivered — note that a stale notify.sh" >&2
    echo "    on the host is one of the things this check looks for." >&2
  fi
else
  echo "==> $NOTIFY is missing or not executable; drift was NOT alerted" >&2
fi

exit 1
