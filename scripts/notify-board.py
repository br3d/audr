#!/usr/bin/env python3
"""Open (and later close) a Paperclip issue for an ongoing outage — AUD-444.

This is the alert sink the founder chose on AUD-444, over Telegram, email and a
hosted uptime prober. The reasoning is worth keeping: the AUD-443 outage was two
hours of nobody-noticing time, and a chat message only helps if a human happens
to be looking at the chat. A `critical` issue on the board lands where the team
already looks AND wakes its assignee, so the thing that notices is an agent on a
timer rather than someone's attention.

It is reached through scripts/notify.sh, which is the entry point every caller
uses; nothing calls this directly except its own tests.

Python rather than bash because this sink talks JSON both ways. Building request
bodies and reading `id`/`status` back out of responses in shell would mean
either a jq dependency the deploy host does not have, or hand-rolled quoting
around alert text that contains newlines, quotes and emoji. stdlib only, so
there is nothing to install on the host either.

WHY THERE IS STATE
------------------
The watchdog repeats while the stand is still down (hourly, by design — see
stand-watchdog.sh). Doing the naive thing and POSTing a new issue per alert
would turn one outage into a dozen `critical` issues and make the board less
useful than the silence it replaced. So `--key` names the ongoing condition and
one file per key remembers the issue already opened for it:

    first alert        -> create the issue, record its id
    repeat alerts      -> comment on that issue
    --resolve          -> comment "recovered", close it, forget the id

The recorded id is re-checked before it is reused: if someone already closed the
issue by hand, the next alert opens a fresh one instead of commenting into a
resolved thread nobody is watching.

EXIT CODES
----------
notify.sh needs to tell "this channel is not set up" apart from "this channel is
set up and broke", because those call for different responses, so the codes are
a contract with it rather than plain success/failure:

    0  delivered (or --resolve with nothing open, which needs no issue)
    1  configured, but the alert could not be delivered
    2  refused — empty message
    3  no credentials; nothing was attempted

Deciding what a non-zero code means is notify.sh's job, not this script's. It
holds --strict and defaults to swallowing failures, for the reason given in its
header: every caller is already on a failure path, so an alerting helper that
escalates "the stand is down" into "the alerting helper also broke" is worse
than no alerting at all.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# NOT `from datetime import UTC`: that alias is 3.11+, and these scripts run
# under whatever `python3` the host shell provides — the deploy host is 3.10.12,
# where importing it is a hard ImportError on every probe. Same hazard the
# target-version note in ruff.toml describes. Hence the UP017 suppression below.
from datetime import datetime, timezone

# Statuses that mean "this issue is finished" — a recorded id in one of these is
# not a thread to append to. Anything else (todo/in_progress/blocked/...) counts
# as still open, so an unfamiliar status errs towards commenting rather than
# opening a duplicate.
CLOSED_STATUSES = {"done", "cancelled", "canceled"}

ATTEMPTS = 3
TIMEOUT = 15
TITLE_MAX = 120

# Credentials and routing. All but the first three are optional; the issue is
# still created without an assignee or project, it just lands unrouted.
REQUIRED = ("PAPERCLIP_API_URL", "PAPERCLIP_API_KEY", "PAPERCLIP_COMPANY_ID")
OPTIONAL = ("AUDR_ALERT_ASSIGNEE_AGENT_ID", "AUDR_ALERT_PROJECT_ID", "AUDR_ALERT_PRIORITY")


def log(msg: str, *, err: bool = False) -> None:
    print(f"[notify-board] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


def load_env_file(path: str) -> dict[str, str]:
    """Read KEY=VALUE lines out of a shell-style credentials file.

    Deliberately not a shell `source`: this file holds an API key, and sourcing
    it would execute whatever else ended up in there. Only the handful of names
    this script knows about are taken, so a stray line cannot inject config.
    """
    found: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                line = line.removeprefix("export ").strip()
                key, _, value = line.partition("=")
                key = key.strip()
                if key not in REQUIRED + OPTIONAL:
                    continue
                value = value.strip()
                # Tolerate quoting, since this file is usually written by hand.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                found[key] = value
    except OSError:
        return {}
    return found


def resolve_config(repo_dir: str) -> dict[str, str]:
    """Environment first, credentials file second — same precedence as notify.sh."""
    cfg = {k: v for k in REQUIRED + OPTIONAL if (v := os.environ.get(k, "").strip())}
    if all(cfg.get(k) for k in REQUIRED):
        return cfg

    remote = os.environ.get("AUDR_REMOTE_DIR", "").strip()
    candidates = [
        os.environ.get("AUDR_BOARD_ENV", "").strip(),
        os.path.join(remote, "secrets", "paperclip.env") if remote else "",
        os.path.join(repo_dir, "secrets", "paperclip.env"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            # File values fill gaps only; an explicit environment value wins.
            for key, value in load_env_file(candidate).items():
                cfg.setdefault(key, value)
            break
    return cfg


def api_base(url: str) -> str:
    """Normalise to the origin, so both `.../` and `.../api` spellings work.

    Also rejects anything that is not http(s). The base URL comes from a
    credentials file, and urlopen would otherwise happily accept `file:` — a
    typo there should fail loudly rather than quietly read a local path.
    """
    base = url.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    scheme = urllib.parse.urlsplit(base).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"PAPERCLIP_API_URL must be http(s), got {scheme or '<none>'}://")
    return base


def request(cfg: dict[str, str], method: str, path: str, payload: dict | None) -> tuple[int, dict]:
    """One API call, retried. Returns (status, parsed-body-or-empty).

    Retries transport errors and 5xx only. A 4xx is a bad token or a bad
    payload; repeating it just delays the fallback to logging the alert.
    """
    url = api_base(cfg["PAPERCLIP_API_URL"]) + path
    body = json.dumps(payload).encode() if payload is not None else None
    last = "no attempt made"

    for attempt in range(1, ATTEMPTS + 1):
        req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 - api_base()
        req.add_header("Authorization", f"Bearer {cfg['PAPERCLIP_API_KEY']}")
        req.add_header("Content-Type", "application/json")
        try:
            # noqa justified: api_base() has already rejected any non-http(s) scheme.
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = {}
                return resp.status, parsed if isinstance(parsed, dict) else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code < 500:
                return exc.code, {"error": detail}
            last = f"HTTP {exc.code} {detail}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # The host is by definition in a bad state when this runs, so one
            # blipped request is expected rather than conclusive.
            last = f"{type(exc).__name__}: {exc}"

        log(f"attempt {attempt}/{ATTEMPTS} failed ({last})", err=True)
        if attempt < ATTEMPTS:
            time.sleep(attempt * 3)

    return 0, {"error": last}


def state_path(state_dir: str, key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", key) or "default"
    return os.path.join(state_dir, f"issue-{safe}")


def read_open_issue(cfg: dict[str, str], path: str) -> str | None:
    """The issue id recorded for this key, if it is still open."""
    try:
        with open(path, encoding="utf-8") as fh:
            issue_id = fh.read().strip()
    except OSError:
        return None
    if not issue_id:
        return None

    status_code, body = request(cfg, "GET", f"/api/issues/{issue_id}", None)
    if status_code == 404:
        log(f"recorded issue {issue_id} no longer exists — will open a new one")
        return None
    if status_code != 200:
        # Unknown: prefer commenting on the recorded issue over risking a
        # duplicate `critical` issue for an outage already reported.
        log(
            f"could not confirm issue {issue_id} (HTTP {status_code}) — assuming still open",
            err=True,
        )
        return issue_id

    issue = body.get("issue", body)
    state = str(issue.get("status", "")).lower()
    if state in CLOSED_STATUSES:
        log(f"recorded issue {issue_id} is {state} — will open a new one")
        return None
    return issue_id


def title_for(message: str, stamp: str) -> str:
    first = next((ln.strip() for ln in message.splitlines() if ln.strip()), "audr alert")
    room = TITLE_MAX - len(stamp) - 3
    if len(first) > room:
        first = first[: room - 1].rstrip() + "…"
    return f"{first} — {stamp}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True, description=__doc__)
    parser.add_argument("--key", default="", help="names the ongoing condition, for dedupe")
    parser.add_argument(
        "--resolve", action="store_true", help="the condition has cleared; close the issue"
    )
    parser.add_argument("--state-dir", default="", help="override where recorded issue ids live")
    parser.add_argument("message", nargs="*", help="alert body; read from stdin when omitted")
    args = parser.parse_args(argv)

    message = " ".join(args.message) if args.message else sys.stdin.read()
    message = message.strip()
    if not message:
        log("refusing to send an empty message", err=True)
        return 2

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = resolve_config(repo_dir)
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        log(f"no board channel configured ({', '.join(missing)} unset)")
        log("message NOT sent:")
        for line in message.splitlines():
            log(f"  {line}")
        return 3

    # Fail the URL once, here, rather than letting a ValueError out of the
    # first request() call: a misconfigured base URL must still end with the
    # alert text in the log, not a traceback on top of an outage.
    try:
        api_base(cfg["PAPERCLIP_API_URL"])
    except ValueError as exc:
        log(f"{exc} — message NOT sent:", err=True)
        for line in message.splitlines():
            log(f"  {line}", err=True)
        return 1

    state_dir = args.state_dir or os.environ.get(
        "AUDR_NOTIFY_STATE_DIR",
        os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
            "audr-notify",
        ),
    )
    path = state_path(state_dir, args.key) if args.key else ""
    existing = read_open_issue(cfg, path) if path else None
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017

    if args.resolve:
        if not existing:
            # Nothing was ever escalated, so there is nothing to stand down
            # from. Opening an issue to announce that all is well is noise.
            log("nothing open for this key — recovery needs no issue")
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return 0

        # One call, not a comment followed by a close: PATCH carries the
        # recovery note itself, and doing both would post the same text twice.
        status_code, _ = request(
            cfg, "PATCH", f"/api/issues/{existing}", {"status": "done", "comment": message}
        )
        if status_code in (200, 201):
            log(f"issue {existing} closed — recovery recorded")
            try:
                os.remove(path)
            except OSError:
                pass
            return 0
        # Leave the state file in place: the issue is still open, so the next
        # alert should keep commenting on it rather than open a second one.
        log(f"could not close issue {existing} (HTTP {status_code})", err=True)
        return 1

    if existing:
        status_code, _ = request(cfg, "POST", f"/api/issues/{existing}/comments", {"body": message})
        if status_code in (200, 201):
            log(f"commented on open issue {existing}")
            return 0
        log(f"comment on {existing} failed (HTTP {status_code})", err=True)
        return 1

    payload: dict[str, object] = {
        "title": title_for(message, stamp),
        "description": message,
        "status": "todo",
        "priority": cfg.get("AUDR_ALERT_PRIORITY") or "critical",
    }
    if cfg.get("AUDR_ALERT_ASSIGNEE_AGENT_ID"):
        payload["assigneeAgentId"] = cfg["AUDR_ALERT_ASSIGNEE_AGENT_ID"]
    if cfg.get("AUDR_ALERT_PROJECT_ID"):
        payload["projectId"] = cfg["AUDR_ALERT_PROJECT_ID"]

    status_code, body = request(
        cfg, "POST", f"/api/companies/{cfg['PAPERCLIP_COMPANY_ID']}/issues", payload
    )
    if status_code not in (200, 201):
        log(f"GAVE UP — could not open an issue (HTTP {status_code}). Message follows:", err=True)
        for line in message.splitlines():
            log(f"  {line}", err=True)
        return 1

    issue = body.get("issue", body)
    issue_id = str(issue.get("id", "")).strip()
    log(f"opened issue {issue_id or '<id missing from response>'} ({payload['priority']})")

    if path and issue_id:
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(issue_id + "\n")
        except OSError as exc:
            # Non-fatal, but worth saying loudly: without the record the next
            # repeat alert opens a second issue for the same outage.
            log(f"could not record issue id in {path} ({exc}) — repeats will duplicate", err=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
