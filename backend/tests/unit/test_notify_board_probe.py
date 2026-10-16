"""`notify-board.py --probe` — the check that replaced a lie (AUD-594).

scripts/install-watchdog.sh used to finish with "Alert credentials present —
alerts will be delivered." on the strength of `test -f secrets/paperclip.env`.
On the stand the file was there and the key inside it had expired, so every
alert spent a 401 and then spooled: delivery still happened by the slow
spool->drain path, but the operator had been told the host delivers directly,
within the probe window. `--probe` is what makes that sentence checkable, so
these tests exist to keep it honest — each one pins a verdict that, if it
regressed, would read as "alerting works" while it did not.

They live in the backend suite rather than next to the script because
`scripts/test_*.py` is run by no CI job (backend/pyproject.toml pins
`testpaths = ["tests"]`), and an unrun test is worth less than no test: it reads
as coverage. compose.test.yaml mounts scripts/notify-board.py for this reason,
the same way AUD-580 mounted the watchdog.

The HTTP layer is stubbed, so nothing here reaches a real board.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "notify-board.py"

if not _SCRIPT.exists():  # pragma: no cover - mount missing
    pytest.skip(
        "scripts/notify-board.py is not mounted into the test container",
        allow_module_level=True,
    )

_SPEC = importlib.util.spec_from_file_location("notify_board_probe", _SCRIPT)
assert _SPEC and _SPEC.loader
nb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nb)

# The key is in here so the "never logged" assertions have something real to
# look for: a probe that printed the credential it was testing would put it in
# every install log and every CI job that runs --check.
CONFIGURED = {
    "PAPERCLIP_API_URL": "https://board.example/api",
    "PAPERCLIP_API_KEY": "probe-key-never-logged",
    "PAPERCLIP_COMPANY_ID": "company-1",
}


class _FakeApi:
    """Replies with one scripted (status, body) and records every call."""

    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, _cfg: dict[str, str], method: str, path: str, _payload: dict | None
    ) -> tuple[int, dict]:
        self.calls.append((method, path))
        return self.status, self.body


def _probe(
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict | None = None
) -> tuple[int, _FakeApi]:
    api = _FakeApi(status, body)
    monkeypatch.setattr(nb, "request", api)
    return nb.probe(dict(CONFIGURED)), api


def test_unconfigured_is_its_own_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """3, not 1 — and without touching the network.

    notify.sh branches on these two codes differently ("set up and broke" vs
    "never set up"), and so does the installer's report: one needs a replaced
    key, the other needs a key at all.
    """
    api = _FakeApi(200)
    monkeypatch.setattr(nb, "request", api)

    assert nb.probe({"PAPERCLIP_API_URL": "https://board.example"}) == 3
    assert api.calls == []


def test_a_credential_that_answers_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    rc, api = _probe(monkeypatch, 200)

    assert rc == 0
    assert api.calls == [("GET", "/api/agents/me")]


def test_probe_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only, by construction.

    A probe that proved delivery by delivering would leave a `critical` issue
    behind on every install and every --check — AUD-570's mistake with extra
    steps.
    """
    _, api = _probe(monkeypatch, 200)

    assert [m for m, _ in api.calls] == ["GET"]


def test_an_expired_key_is_rejected_and_explained(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact observed symptom: HTTP 401, "Expired agent token".

    This is the case the old `test -f` reported as a working channel.
    """
    rc, _ = _probe(
        monkeypatch,
        401,
        {"error": "Expired agent token; obtain fresh credentials and retry"},
    )

    assert rc == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "REJECTED" in combined
    assert "401" in combined
    # The server's reason has to survive into the output: "rejected" alone sends
    # the reader to the host to find out it was simply expired.
    assert "Expired agent token" in combined
    assert CONFIGURED["PAPERCLIP_API_KEY"] not in combined


def test_a_scope_limited_key_is_not_reported_as_broken(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """403 means authenticated-but-narrow, which is what this file should hold.

    The credential the install note asks for is a `task_bridge` key scoped to
    opening one agent's issues, and such a key can legitimately be refused on
    /api/agents/me while being able to POST the alert. Calling that "broken"
    would push an operator into replacing a working key — a false alarm in the
    opposite direction, and just as costly. An expired or wrong key answers 401.
    """
    rc, _ = _probe(monkeypatch, 403, {"error": "forbidden"})

    assert rc == 0
    assert "403" in capsys.readouterr().out


def test_an_unreachable_board_is_not_called_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """request() returns status 0 when every attempt failed transport-level."""
    rc, _ = _probe(monkeypatch, 0, {"error": "URLError: timed out"})

    assert rc == 1


def test_probe_mode_needs_no_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--probe must not block on stdin.

    `message` is `nargs="*"` and falls back to reading stdin, so a probe routed
    through the normal path would hang when run from the installer's ssh session
    with no input attached. Asserted with a stdin that raises rather than one
    that returns "", because an empty read would merely exit 2 and look like a
    tidy refusal instead of the wedge it is.
    """

    class _Exploding:
        def read(self) -> str:
            raise AssertionError("--probe read stdin")

    monkeypatch.setattr(sys, "stdin", _Exploding())
    monkeypatch.setattr(nb, "resolve_config", lambda _repo: dict(CONFIGURED))
    monkeypatch.setattr(nb, "request", _FakeApi(200))

    assert nb.main(["--probe"]) == 0
    assert "PROBE" not in capsys.readouterr().err


def test_dry_run_reports_a_credential_that_would_be_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A drill against an expired key must say so (AUD-594).

    Before this, the board half of `notify.sh --dry-run` reported a healthy sink
    whenever the file parsed: the dedupe pre-read only calls the API when a state
    file already exists for the key, so a fresh key made zero requests and the
    drill printed "would open a new critical issue". The tool whose job is to
    tell you whether alerting works was the last place that would have caught
    the expired token.

    The exit code deliberately stays 0: notify.sh reads a non-zero board result
    as a delivery failure, and a drill must not render as a real one.
    """
    monkeypatch.setattr(nb, "resolve_config", lambda _repo: dict(CONFIGURED))
    monkeypatch.setattr(nb, "request", _FakeApi(401, {"error": "Expired agent token"}))

    rc = nb.main(["--dry-run", "--key", "probe-test", "--state-dir", "/nonexistent", "drill"])

    assert rc == 0
    combined = capsys.readouterr()
    assert "would NOT have accepted" in combined.err
    assert "DRY RUN" in combined.out
