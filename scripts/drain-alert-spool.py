#!/usr/bin/env python3
"""Deliver the alerts the deploy host could not send itself — AUD-444.

The deploy host runs the watchdog but holds no board credential, and it is not
supposed to get one: minting a long-lived `task_bridge` key needs a human board
user, and the token an agent could copy there instead expires in 48 hours, so it
would fail silently on the one day it mattered (docs/deploy-runbook.md). So
`scripts/notify.sh` parks anything it could not deliver in a spool file, and
this script replays that file from somewhere a board credential already exists:
inside an agent run, whose environment carries one for the length of the run.

The trade is latency, not delivery. A spooled alert arrives at the next agent
heartbeat rather than within the ~5-minute probe window. That is worse than a
credential on the host and much better than the journal-only status quo, and it
needs nobody's permission to turn on. When `secrets/paperclip.env` does land on
the host, the spool simply stops filling and this script finds nothing to do.

    python3 scripts/drain-alert-spool.py                 # deliver and clear
    python3 scripts/drain-alert-spool.py --dry-run       # show, change nothing

Credentials come from the environment, exactly as `notify-board.py` reads them
(PAPERCLIP_API_URL / PAPERCLIP_API_KEY / PAPERCLIP_COMPANY_ID); delivery itself
is delegated to that script so the issue-per-outage lifecycle has one
implementation rather than two.

ONE ISSUE PER OUTAGE STILL HOLDS
--------------------------------
The watchdog repeats hourly while the stand is down, so a spool drained a day
later can hold a dozen records for the same condition. Replaying them verbatim
would put a dozen comments on one issue and bury the current state. Instead each
`--key` is collapsed to the newest alert for it, plus a trailing recovery if one
arrived after that — so a finished outage reads as one issue opened and closed,
and an ongoing one as one issue still open with its latest probe count.

An alert delivered late is labelled as such. Without that, a replayed
"🔴 audr stand DOWN" reads as a live outage and sends somebody to the host for
an incident that ended yesterday.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

DEFAULT_HOST = "codex@192.168.1.228"
DEFAULT_SSH_KEY = "/home/codex/git/audr/id_ed25519"
DEFAULT_STATE_DIR = "~/.local/state/audr-notify"
SPOOL_NAME = "undelivered.jsonl"
# notify.sh appends to SPOOL_NAME; a drain moves it aside first so alerts
# arriving mid-drain are not lost to the truncate. A `.draining` left behind by
# an interrupted run is picked up by the next one rather than orphaned.
CLAIM_NAME = "undelivered.jsonl.draining"

NOTIFY_BOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify-board.py")


def ssh_base(host: str, key: str) -> list[str]:
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15"]
    if key:
        cmd += ["-i", key]
    return [*cmd, host]


def run_remote(host: str, key: str, script: str) -> subprocess.CompletedProcess:
    """Run a shell script on the host, fed through `bash -s` on stdin.

    stdin is the script itself, so anything the remote side has to *read* —
    spool contents on the way back — travels in a heredoc inside it rather than
    as a second stream.
    """
    return subprocess.run(
        [*ssh_base(host, key), "bash -s"],
        input=script,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def remote_dir_expr(state_dir: str) -> str:
    """Quote a remote directory for the shell *without* killing `~` expansion.

    shlex.quote wraps the whole path in single quotes, which is correct for
    hostile characters and silently wrong for the default `~/.local/state/...`:
    the remote bash then looks for a literal directory named `~`, finds
    nothing, and the drain reports an empty spool while alerts pile up on the
    host. So the tilde is handled as `"$HOME"` and only the remainder is quoted.
    """
    if state_dir == "~":
        return '"$HOME"'
    if state_dir.startswith("~/"):
        return '"$HOME"/' + shlex.quote(state_dir[2:])
    return shlex.quote(state_dir)


def claim_spool(host: str, key: str, state_dir: str, dry_run: bool) -> str:
    """Move the spool aside (unless --dry-run) and return its contents.

    The merge-then-read is one remote shell so it cannot interleave with the
    watchdog appending between two round trips.
    """
    quoted = remote_dir_expr(state_dir)
    if dry_run:
        # Read both files without touching either, so a dry run is honest about
        # what a real drain would pick up, including a stale claim.
        script = f"""
set -uo pipefail
d={quoted}
cat "$d/{CLAIM_NAME}" 2>/dev/null || true
cat "$d/{SPOOL_NAME}" 2>/dev/null || true
"""
    else:
        script = f"""
set -uo pipefail
d={quoted}
[ -d "$d" ] || exit 0
# Append the live spool onto any stale claim, oldest first, then read the claim.
if [ -f "$d/{SPOOL_NAME}" ]; then
  cat "$d/{SPOOL_NAME}" >> "$d/{CLAIM_NAME}" && rm -f "$d/{SPOOL_NAME}"
fi
cat "$d/{CLAIM_NAME}" 2>/dev/null || true
"""
    proc = run_remote(host, key, script)
    if proc.returncode != 0:
        raise SystemExit(f"[drain] cannot reach the spool on {host}: {proc.stderr.strip()}")
    return proc.stdout


def parse_records(blob: str) -> list[dict]:
    records = []
    for lineno, line in enumerate(blob.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # A truncated final line is the expected corruption here (the host
            # was mid-append when it died). Report it and keep the rest: losing
            # every alert because one is malformed is the wrong trade.
            print(f"[drain] skipping unparseable spool line {lineno}", file=sys.stderr)
            continue
        if isinstance(record, dict) and record.get("message"):
            record["_line"] = lineno
            records.append(record)
    return records


def plan(records: list[dict]) -> list[tuple[dict, list[int]]]:
    """Collapse the spool into the sends worth making.

    Returns (record, source_line_numbers) pairs in delivery order. The source
    lines are carried so a send that fails can put *all* the records it stood
    for back on the spool, rather than silently discarding the ones it folded.
    """
    keyed: dict[str, list[dict]] = {}
    sends: list[tuple[dict, list[int]]] = []
    for record in records:
        alert_key = record.get("key") or ""
        if not alert_key:
            # No key means no ongoing condition to dedupe against, so these
            # stand alone and each one is replayed.
            sends.append((record, [record["_line"]]))
        else:
            keyed.setdefault(alert_key, []).append(record)

    for alert_key, group in keyed.items():
        lines = [r["_line"] for r in group]
        alerts = [r for r in group if not r.get("resolve")]
        resolves = [r for r in group if r.get("resolve")]
        if alerts:
            latest = alerts[-1]
            # Attribute the whole group to the first send; the resolve below
            # only has to re-spool itself if it is the part that fails.
            sends.append(
                (latest, [ln for ln in lines if ln != resolves[-1]["_line"]] if resolves else lines)
            )
            if resolves and resolves[-1]["_line"] > latest["_line"]:
                sends.append((resolves[-1], [resolves[-1]["_line"]]))
        elif resolves:
            # Recovery with no outage alert in the spool: the opening alert was
            # delivered before the channel broke, so this still has an issue to
            # close. notify-board.py treats "nothing open" as a no-op.
            sends.append((resolves[-1], lines))
        print(f"[drain] key={alert_key}: {len(group)} spooled record(s) collapsed", file=sys.stderr)

    sends.sort(key=lambda pair: pair[0]["_line"])
    return sends


def replay_body(record: dict) -> str:
    """Label the alert as a late replay, then quote it unchanged.

    The first line carries the original alert's own first line, because
    notify-board.py derives the issue title from it: leading with the
    explanation instead gives the board a row that reads "Delayed alert replay —
    the deploy host spooled this at…" and says nothing about what broke. So the
    title keeps the alert's own words and only marks that it arrived late; the
    reason it was late goes underneath, where it costs nobody a scan.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    message = record["message"]
    first = next((ln.strip() for ln in message.splitlines() if ln.strip()), "audr alert")
    return (
        f"⏱ (delayed replay) {first}\n"
        "\n"
        f"The deploy host spooled this at {record.get('ts', '?')} and it was delivered at {now}. "
        f"The host has no board credential ({record.get('reason', 'undelivered')}), so the alert "
        "waited for an agent heartbeat instead of arriving within the probe window.\n"
        "**Check the current state before acting on it — this may describe an outage that has "
        "already ended.** See AUD-444.\n"
        "\n"
        "--- original alert ---\n"
    ) + message


def deliver(record: dict, dry_run: bool) -> bool:
    args = [sys.executable, NOTIFY_BOARD]
    if record.get("key"):
        args += ["--key", str(record["key"])]
    if record.get("resolve"):
        args.append("--resolve")
    args += ["--", replay_body(record)]

    if dry_run:
        kind = "resolve" if record.get("resolve") else "alert"
        first = record["message"].splitlines()[0] if record["message"] else ""
        print(f"[drain] would send {kind} key={record.get('key') or '-'}: {first}")
        return True

    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.returncode == 0:
        return True
    if proc.returncode == 3:
        # No credentials in *this* environment either. Not a per-record failure
        # and retrying the rest cannot help, so say so plainly and stop.
        raise SystemExit(
            "[drain] no board credential in this environment — set PAPERCLIP_API_URL, "
            "PAPERCLIP_API_KEY and PAPERCLIP_COMPANY_ID. The spool is untouched."
        )
    sys.stderr.write(proc.stderr)
    print(
        f"[drain] delivery failed (exit {proc.returncode}) for key={record.get('key') or '-'}",
        file=sys.stderr,
    )
    return False


def write_back(host: str, key: str, state_dir: str, records: list[dict], lines: set[int]) -> None:
    """Leave exactly the undelivered records on the claim file.

    Anything delivered is dropped; anything not is kept for the next heartbeat.
    An empty remainder removes the file, so a clean drain leaves no state.
    """
    keep = [r for r in records if r["_line"] in lines]
    quoted = remote_dir_expr(state_dir)
    if not keep:
        proc = run_remote(host, key, f"set -uo pipefail\nrm -f {quoted}/{CLAIM_NAME}\n")
    else:
        payload = "".join(
            json.dumps({k: v for k, v in r.items() if k != "_line"}, ensure_ascii=False) + "\n"
            for r in keep
        )
        # A quoted heredoc, so the alert bodies — which carry quotes, newlines
        # and emoji — reach the file byte for byte with no shell expansion.
        # JSON encoding guarantees no line can be the terminator itself.
        proc = run_remote(
            host,
            key,
            f"set -uo pipefail\nmkdir -p {quoted}\ncat > {quoted}/{CLAIM_NAME} <<'AUDR_SPOOL_EOF'\n"
            f"{payload}AUDR_SPOOL_EOF\n",
        )
    if proc.returncode != 0:
        print(
            f"[drain] could not update the spool on {host}: {proc.stderr.strip()}", file=sys.stderr
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default=os.environ.get("AUDR_DEPLOY_HOST", DEFAULT_HOST))
    parser.add_argument("--ssh-key", default=os.environ.get("AUDR_SSH_KEY", DEFAULT_SSH_KEY))
    parser.add_argument(
        "--state-dir", default=os.environ.get("AUDR_REMOTE_NOTIFY_STATE_DIR", DEFAULT_STATE_DIR)
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be sent, change nothing"
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(NOTIFY_BOARD):
        raise SystemExit(f"[drain] {NOTIFY_BOARD} is missing — cannot deliver anything")

    records = parse_records(claim_spool(args.host, args.ssh_key, args.state_dir, args.dry_run))
    if not records:
        print("[drain] spool is empty — nothing to deliver")
        return 0

    sends = plan(records)
    print(f"[drain] {len(records)} spooled record(s) -> {len(sends)} send(s)")

    undelivered: set[int] = set()
    for record, source_lines in sends:
        if not deliver(record, args.dry_run):
            undelivered.update(source_lines)

    if args.dry_run:
        print("[drain] dry run — the spool on the host is unchanged")
        return 0

    write_back(args.host, args.ssh_key, args.state_dir, records, undelivered)
    delivered = len(records) - len(undelivered)
    print(f"[drain] delivered {delivered} record(s); {len(undelivered)} left for the next drain")
    return 1 if undelivered else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
