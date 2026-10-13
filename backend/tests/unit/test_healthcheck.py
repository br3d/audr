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

import time
from contextlib import contextmanager
from pathlib import Path
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


# --- `--worker` mode (AUD-539) ---------------------------------------------
#
# The worker has no HTTP server to probe, so its healthcheck reads a local
# timestamp file the worker loop ticks on a fixed cadence — not the database,
# so a DB outage the worker cannot fix by restarting never shows up here (the
# same "my own loop is beating, not the DB is reachable" rule /health/live
# follows for the api container).


def test_worker_fresh_beat_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    beat_file = tmp_path / "heartbeat"
    beat_file.write_text(str(time.time()))
    monkeypatch.setattr(healthcheck, "WORKER_HEARTBEAT_PATH", beat_file)
    assert healthcheck.main(["--worker"]) == 0


def test_worker_stale_beat_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    beat_file = tmp_path / "heartbeat"
    stale_at = time.time() - healthcheck.WORKER_STALE_SECONDS - 1
    beat_file.write_text(str(stale_at))
    monkeypatch.setattr(healthcheck, "WORKER_HEARTBEAT_PATH", beat_file)
    assert healthcheck.main(["--worker"]) == 1
    out = capsys.readouterr().out
    assert "Traceback" not in out


def test_worker_missing_beat_file_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    beat_file = tmp_path / "never-written"
    monkeypatch.setattr(healthcheck, "WORKER_HEARTBEAT_PATH", beat_file)
    assert healthcheck.main(["--worker"]) == 1
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert out.strip() != ""


def test_worker_garbage_beat_file_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A half-written or corrupt beat file is one readable line, not a crash."""
    beat_file = tmp_path / "heartbeat"
    beat_file.write_text("not-a-timestamp")
    monkeypatch.setattr(healthcheck, "WORKER_HEARTBEAT_PATH", beat_file)
    assert healthcheck.main(["--worker"]) == 1
    out = capsys.readouterr().out
    assert "Traceback" not in out


def test_worker_mode_does_not_probe_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--worker` must never fall through to the api's urlopen probe."""

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("worker mode must not hit the HTTP liveness probe")

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", boom)
    beat_file = tmp_path / "heartbeat"
    beat_file.write_text(str(time.time()))
    monkeypatch.setattr(healthcheck, "WORKER_HEARTBEAT_PATH", beat_file)
    assert healthcheck.main(["--worker"]) == 0


# --- compose.yaml / release.sh coupling (AUD-442) -------------------------
#
# The inline copy in compose.yaml is temporary by construction, and the thing
# that ends it is `scripts/release.sh`, which rewrites the block when it cuts a
# tag that carries this module. Both files are anchored on one comment line, so
# rewording that comment would silently turn the revert into a no-op and ship
# the inline copy forever. These three tests are what notices.

REPO_ROOT = Path(__file__).resolve().parents[3]
INLINE_ANCHOR = "# Inlined, and only until the next publish"
MODULE_FORM = 'test: ["CMD", "python", "-m", "audr.operations.healthcheck"]'


def _compose() -> str:
    return (REPO_ROOT / "compose.yaml").read_text()


def _release() -> str:
    return (REPO_ROOT / "scripts/release.sh").read_text()


def _api_healthcheck_test() -> str:
    """The `test:` value of the api service's healthcheck, comments removed.

    Every assertion below has to read YAML rather than prose: the comment block
    above the inline copy quotes both the module invocation and `/health/ready`
    while explaining why it uses neither, so a plain `in compose` check sees the
    opposite of the truth. Comments are stripped here once instead.
    """
    compose = _compose()
    start = compose.index("    healthcheck:", compose.index("\n  api:"))
    end = compose.index("      interval:", start)
    return "\n".join(
        line for line in compose[start:end].splitlines() if not line.lstrip().startswith("#")
    )


def test_compose_healthcheck_is_one_of_the_two_accepted_forms() -> None:
    block = _api_healthcheck_test()
    inline = "python" in block and "-c" in block
    module = MODULE_FORM in block
    assert inline != module, (
        "compose.yaml's api healthcheck must be either the module form "
        f"({MODULE_FORM}) or an inline `python -c` probe — found "
        f"inline={inline}, module={module}. scripts/release.sh only knows how "
        f"to convert the second into the first.\n{block}"
    )


def test_release_script_restores_the_module_form() -> None:
    """The next release must drop the inline copy without anyone remembering to."""
    release = _release()
    if MODULE_FORM in _api_healthcheck_test():
        assert "audr.operations.healthcheck" in release, (
            "compose.yaml already calls the module, but scripts/release.sh "
            "dropped the guard that fails the release if that line goes missing."
        )
        return
    assert INLINE_ANCHOR in _compose(), (
        "compose.yaml inlines the probe but no longer marks it with "
        f"{INLINE_ANCHOR!r} — that comment is the anchor scripts/release.sh "
        "matches on, so the release-time revert would silently become a no-op."
    )
    assert INLINE_ANCHOR in release, (
        "compose.yaml marks its api healthcheck as temporary but "
        f"scripts/release.sh no longer anchors on {INLINE_ANCHOR!r}, so cutting "
        "a release would leave the inline copy in place against a tag that "
        "does carry the module."
    )
    assert MODULE_FORM in release, (
        "scripts/release.sh anchors on the inline block but does not write "
        f"{MODULE_FORM} in its place."
    )


def test_inline_copy_obeys_the_modules_rules() -> None:
    """While the inline copy exists it must stay equivalent to this module."""
    block = _api_healthcheck_test()
    if MODULE_FORM in block:
        pytest.skip("compose.yaml calls the module directly; nothing to mirror")
    assert healthcheck.URL in block, f"inline probe must hit {healthcheck.URL}"
    assert "/health/ready" not in block, "readiness would restart the container for a DB outage"
    assert "curl" not in block, "curl is not in the runtime image (docs/third-party.md 1.3)"
    assert "urllib.request" in block, "stdlib only"
    assert "print(" in block, "print one line; a traceback floods .State.Health.Log"


# --- compose.yaml / release.sh coupling, worker (AUD-539) ------------------
#
# Same dance as the api block above, for the same reason: compose.yaml pins a
# published tag (0.1.2) that predates `--worker`, so it inlines a stdlib
# equivalent of `_check_worker` until a tag that carries the flag ships.

WORKER_INLINE_ANCHOR = "# Inlined (worker), and only until the next publish"
WORKER_MODULE_FORM = 'test: ["CMD", "python", "-m", "audr.operations.healthcheck", "--worker"]'


def _worker_healthcheck_test() -> str:
    """The `test:` value of the worker service's healthcheck, comments removed."""
    compose = _compose()
    start = compose.index("    healthcheck:", compose.index("\n  worker:"))
    end = compose.index("      interval:", start)
    return "\n".join(
        line for line in compose[start:end].splitlines() if not line.lstrip().startswith("#")
    )


def test_compose_worker_healthcheck_is_one_of_the_two_accepted_forms() -> None:
    block = _worker_healthcheck_test()
    inline = "python" in block and "-c" in block
    module = WORKER_MODULE_FORM in block
    assert inline != module, (
        "compose.yaml's worker healthcheck must be either the module form "
        f"({WORKER_MODULE_FORM}) or an inline `python -c` probe — found "
        f"inline={inline}, module={module}. scripts/release.sh only knows how "
        f"to convert the second into the first.\n{block}"
    )


def test_release_script_restores_the_worker_module_form() -> None:
    """The next release must drop the inline copy without anyone remembering to."""
    release = _release()
    if WORKER_MODULE_FORM in _worker_healthcheck_test():
        assert WORKER_MODULE_FORM in release, (
            "compose.yaml already calls the module, but scripts/release.sh "
            "dropped the guard that fails the release if that line goes missing."
        )
        return
    assert WORKER_INLINE_ANCHOR in _compose(), (
        "compose.yaml inlines the worker probe but no longer marks it with "
        f"{WORKER_INLINE_ANCHOR!r} — that comment is the anchor scripts/release.sh "
        "matches on, so the release-time revert would silently become a no-op."
    )
    assert WORKER_INLINE_ANCHOR in release, (
        "compose.yaml marks its worker healthcheck as temporary but "
        f"scripts/release.sh no longer anchors on {WORKER_INLINE_ANCHOR!r}, so cutting "
        "a release would leave the inline copy in place against a tag that "
        "does carry `--worker`."
    )
    assert WORKER_MODULE_FORM in release, (
        "scripts/release.sh anchors on the inline block but does not write "
        f"{WORKER_MODULE_FORM} in its place."
    )


def test_worker_inline_copy_obeys_the_modules_rules() -> None:
    """While the inline copy exists it must stay equivalent to `_check_worker`."""
    block = _worker_healthcheck_test()
    if WORKER_MODULE_FORM in block:
        pytest.skip("compose.yaml calls the module directly; nothing to mirror")
    assert "worker-heartbeat" in block, "must read the same beacon file the module does"
    assert "/health" not in block, "must not fall back to the HTTP liveness probe"
    assert "curl" not in block, "curl is not in the runtime image (docs/third-party.md 1.3)"
    assert "print(" in block, "print one line; a traceback floods .State.Health.Log"
    assert str(int(healthcheck.WORKER_STALE_SECONDS)) in block, (
        "staleness threshold has drifted from WORKER_STALE_SECONDS"
    )
