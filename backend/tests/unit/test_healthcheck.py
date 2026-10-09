"""The container health check command (AUD-439).

A container health check runs this every 10 seconds, so its contract is narrow
but load-bearing: exit 0 only on a 200, and never let an exception escape — a
traceback would flood `.State.Health.Log`, which Docker caps at 5 entries.

`compose.yaml` currently inlines an equivalent instead of calling the module,
because it pins a published tag that predates it (AUD-442). This stays the
tested reference implementation, and that file reverts to `python -m` once a tag
carrying the module ships — until then, changes here must be mirrored there.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from audr.operations import healthcheck

pytestmark = pytest.mark.unit


@contextmanager
def _response(status: int) -> Any:
    class _Resp:
        def __init__(self) -> None:
            self.status = status

    yield _Resp()


def test_live_api_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda *a, **k: _response(200))
    assert healthcheck.main() == 0


def test_non_200_exits_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda *a, **k: _response(503))
    assert healthcheck.main() == 1
    assert "503" in capsys.readouterr().out


def test_connection_error_prints_one_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One readable line, not a traceback, and no exception out of main()."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("[Errno 111] Connection refused")

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", boom)
    assert healthcheck.main() == 1
    out = capsys.readouterr().out
    assert out.strip() == "[Errno 111] Connection refused"
    assert "Traceback" not in out


def test_probes_liveness_not_readiness() -> None:
    """Readiness touches the DB; an unhealthy container gets restarted, which
    cannot fix a database outage."""
    assert healthcheck.URL == "http://localhost:8000/health/live"
