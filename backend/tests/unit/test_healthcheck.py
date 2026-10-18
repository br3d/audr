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

import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
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


# --- compose.yaml / promote-image.py coupling (AUD-442) -------------------
#
# The inline copy in compose.yaml is temporary by construction, and the thing
# that ends it is `scripts/promote-image.py`, which rewrites the block once the
# tag it is pointing compose.yaml at is verified to be in the registry and
# built from that release commit. (It was `scripts/release.sh` until AUD-546 —
# doing it at cut time assumed the new tag shipped immediately, which is the
# same assumption that left `main` naming a nonexistent image for nine days.)
# Both files are anchored on one comment line, so rewording that comment would
# silently turn the revert into a no-op and ship the inline copy forever.
# These three tests are what notices.

REPO_ROOT = Path(__file__).resolve().parents[3]
INLINE_ANCHOR = "# Inlined, and only until the next publish"
MODULE_FORM = 'test: ["CMD", "python", "-m", "audr.operations.healthcheck"]'


def _compose() -> str:
    return (REPO_ROOT / "compose.yaml").read_text()


def _promote() -> str:
    return (REPO_ROOT / "scripts/promote-image.py").read_text()


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
        f"inline={inline}, module={module}. scripts/promote-image.py only knows how "
        f"to convert the second into the first.\n{block}"
    )


def test_promote_script_restores_the_module_form() -> None:
    """The next promotion must drop the inline copy without anyone remembering to."""
    promote = _promote()
    if MODULE_FORM in _api_healthcheck_test():
        assert "audr.operations.healthcheck" in promote, (
            "compose.yaml already calls the module, but scripts/promote-image.py "
            "dropped the guard that fails the promotion if that line goes missing."
        )
        return
    assert INLINE_ANCHOR in _compose(), (
        "compose.yaml inlines the probe but no longer marks it with "
        f"{INLINE_ANCHOR!r} — that comment is the anchor scripts/promote-image.py "
        "matches on, so the release-time revert would silently become a no-op."
    )
    assert INLINE_ANCHOR in promote, (
        "compose.yaml marks its api healthcheck as temporary but "
        f"scripts/promote-image.py no longer anchors on {INLINE_ANCHOR!r}, so "
        "promoting would leave the inline copy in place against a tag that "
        "does carry the module."
    )
    assert MODULE_FORM in promote, (
        "scripts/promote-image.py anchors on the inline block but does not write "
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


# --- compose.yaml / promote-image.py coupling, worker (AUD-539) ------------
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
        f"inline={inline}, module={module}. scripts/promote-image.py only knows how "
        f"to convert the second into the first.\n{block}"
    )


def test_promote_script_restores_the_worker_module_form() -> None:
    """The next promotion must drop the inline copy without anyone remembering to."""
    promote = _promote()
    if WORKER_MODULE_FORM in _worker_healthcheck_test():
        assert WORKER_MODULE_FORM in promote, (
            "compose.yaml already calls the module, but scripts/promote-image.py "
            "dropped the guard that fails the promotion if that line goes missing."
        )
        return
    assert WORKER_INLINE_ANCHOR in _compose(), (
        "compose.yaml inlines the worker probe but no longer marks it with "
        f"{WORKER_INLINE_ANCHOR!r} — that comment is the anchor scripts/promote-image.py "
        "matches on, so the release-time revert would silently become a no-op."
    )
    assert WORKER_INLINE_ANCHOR in promote, (
        "compose.yaml marks its worker healthcheck as temporary but "
        f"scripts/promote-image.py no longer anchors on {WORKER_INLINE_ANCHOR!r}, so "
        "promoting would leave the inline copy in place against a tag that "
        "does carry `--worker`."
    )
    assert WORKER_MODULE_FORM in promote, (
        "scripts/promote-image.py anchors on the inline block but does not write "
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


def test_worker_inline_copy_passes_when_the_beacon_was_never_written() -> None:
    """The one place the inline copy must NOT mirror the module (AUD-544).

    `_check_worker` exits 1 on a missing beacon file and is right to: it only
    ever runs against a tag `scripts/promote-image.py` has proven carries the
    writer in `audr.jobs.__main__`, so for it a missing file means the worker
    died before its first beat.

    The inline copy has no such guarantee. It runs against whatever published
    tag `x-backend-image` names, and every tag published so far (0.1.1, v0.1.2)
    predates that writer, so the file is simply never created — reading it
    strictly reported a live worker as `unhealthy` on every fresh clone. A
    probe that cannot tell "this image has no beacon" from "the beacon
    stopped" must not fail the first case. Once the file exists, staleness is
    judged exactly as the module judges it.
    """
    block = _worker_healthcheck_test()
    if WORKER_MODULE_FORM in block:
        pytest.skip("compose.yaml calls the module directly; the guarantee holds")
    assert "os.path.exists" in block, (
        "the inline worker probe must test for the beacon file before reading it — "
        "without that, a published tag whose image has no beacon writer reports "
        "`unhealthy` forever against a perfectly live worker (AUD-544)."
    )
    # The absent-file branch must be the *first* exit in the probe, and a
    # success one. Ordering matters: a `sys.exit(1)` reached first is the bug.
    first_exit = block[block.index("sys.exit") :].splitlines()[0]
    assert first_exit.strip() == "sys.exit(0)", (
        f"the first exit in the inline worker probe is {first_exit.strip()!r}; it must "
        "be the `sys.exit(0)` guarding a beacon file that was never written."
    )


# --- the inline copies, actually executed (AUD-607) ------------------------
#
# Everything above this line reads compose.yaml as *text*: it asserts that the
# inline probes mention `urllib.request`, print instead of raising, name the
# right beacon path, and guard the never-written case. That catches drift in
# what the probes are made of, but not in what they do — the snippet is a
# program embedded in YAML that nothing ever runs, so a stray `sys.exit(1)` on
# the success path, an inverted comparison or a plain SyntaxError would ship
# with every one of those assertions still green. Docker would be the first
# thing to execute it, on a fresh clone, every 10 seconds.
#
# So these run it. The extracted source goes to a real interpreter, under each
# state the probe exists to tell apart, and the exit code and output are
# compared against the module's documented contract.
#
# Two literals have to be redirected first: the api probe hardcodes
# `localhost:8000` and the worker probe `/app/worker-heartbeat`, neither of
# which exists in a test runner. Each substitution asserts its own hit count,
# so a probe that stops using the literal fails here rather than silently
# testing a stale string.


def _inline_source(block: str) -> str:
    """The Python program out of a `test: [CMD, python, -c, |]` healthcheck block."""
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "- |":
            body = lines[index + 1 :]
            break
    else:  # pragma: no cover - guarded by the "one of two forms" tests above
        raise AssertionError(f"no `- |` block scalar in healthcheck block:\n{block}")
    indent = len(body[0]) - len(body[0].lstrip())
    return "\n".join(line[indent:] if line.strip() else "" for line in body)


def _run(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, source is our own compose.yaml
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _api_source(port: int) -> str:
    """The api probe, pointed at `port` instead of the container's fixed 8000."""
    source = _inline_source(_api_healthcheck_test())
    redirected = source.replace("localhost:8000", f"localhost:{port}")
    assert redirected != source, (
        "the inline api probe no longer contains the literal `localhost:8000`, so this "
        "test would have executed it against whatever really listens on 8000. Update "
        "the substitution to match the probe."
    )
    return redirected


@contextmanager
def _serving(status: int) -> Any:
    """A one-request HTTP server answering `/health/live` with `status`."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's API
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            """Silence the default stderr access log."""

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _skip_if_module_form(block: str, form: str) -> None:
    if form in block:
        pytest.skip("compose.yaml calls the module directly; the module's own tests cover it")


def test_inline_api_probe_exits_zero_against_a_live_server() -> None:
    _skip_if_module_form(_api_healthcheck_test(), MODULE_FORM)
    with _serving(200) as port:
        result = _run(_api_source(port))
    assert result.returncode == 0, f"a 200 must be healthy; stdout={result.stdout!r}"


def test_inline_api_probe_exits_one_on_an_error_status() -> None:
    """A 5xx is unhealthy — via the `except` arm, not the status comparison.

    Worth being precise about, because it is not the branch it looks like:
    `urlopen` raises `HTTPError` for anything >= 400, so a 503 never reaches
    the `if status != 200` line at all. It is caught, printed as one line and
    exited 1 by the error path — the same path a refused connection takes.
    `test_inline_api_probe_exits_one_on_a_2xx_that_is_not_200` below is what
    covers the comparison itself.
    """
    _skip_if_module_form(_api_healthcheck_test(), MODULE_FORM)
    with _serving(503) as port:
        result = _run(_api_source(port))
    assert result.returncode == 1, "a 503 must be unhealthy"
    assert "503" in result.stdout, (
        f"the failure line must name the status it got; stdout={result.stdout!r}"
    )
    assert "Traceback" not in result.stderr, f"must print, not raise; {result.stderr!r}"


def test_inline_api_probe_exits_one_on_a_2xx_that_is_not_200() -> None:
    """The only input that reaches `if status != 200`, and so the only one that guards it.

    Every other unhealthy case is absorbed by the `except` arm (see above), so
    without a 2xx-but-not-200 the whole comparison — and the `sys.exit(0 if
    status == 200 else 1)` that depends on it — could be replaced by a bare
    `sys.exit(0)` with this file still green. Verified by doing exactly that:
    the mutation survives every other test here and fails only this one.
    """
    _skip_if_module_form(_api_healthcheck_test(), MODULE_FORM)
    with _serving(204) as port:
        result = _run(_api_source(port))
    assert result.returncode == 1, (
        "only a 200 is healthy: a 204 means the app answered without serving the "
        f"liveness body, which is not 'live'; stdout={result.stdout!r}"
    )
    assert "204" in result.stdout, (
        f"the failure line must name the status it got; stdout={result.stdout!r}"
    )


def test_inline_api_probe_prints_one_line_when_nothing_listens() -> None:
    """The pre-startup case: refused connection, one line, no traceback."""
    _skip_if_module_form(_api_healthcheck_test(), MODULE_FORM)
    with _serving(200) as port:
        pass  # the server is closed on exit, so `port` is now free
    result = _run(_api_source(port))
    assert result.returncode == 1, "nothing listening must be unhealthy"
    assert "Traceback" not in result.stderr, (
        "an escaping traceback floods .State.Health.Log, which Docker caps at 5 entries; "
        f"stderr={result.stderr!r}"
    )
    assert len(result.stdout.strip().splitlines()) == 1, (
        f"expected exactly one printed line, got {result.stdout!r}"
    )


def _worker_source(beat_file: Path) -> str:
    """The worker probe, reading `beat_file` instead of /app/worker-heartbeat."""
    source = _inline_source(_worker_healthcheck_test())
    redirected = source.replace("/app/worker-heartbeat", str(beat_file))
    assert source.count("/app/worker-heartbeat") == 2, (
        "expected the inline worker probe to name /app/worker-heartbeat exactly twice "
        "(the existence check and the read); found "
        f"{source.count('/app/worker-heartbeat')}. Update the substitution to match."
    )
    return redirected


def test_inline_worker_probe_exits_zero_on_a_fresh_beat(tmp_path: Path) -> None:
    _skip_if_module_form(_worker_healthcheck_test(), WORKER_MODULE_FORM)
    beat_file = tmp_path / "worker-heartbeat"
    beat_file.write_text(str(time.time()))
    assert _run(_worker_source(beat_file)).returncode == 0


def test_inline_worker_probe_exits_one_on_a_stale_beat(tmp_path: Path) -> None:
    _skip_if_module_form(_worker_healthcheck_test(), WORKER_MODULE_FORM)
    beat_file = tmp_path / "worker-heartbeat"
    stale = time.time() - (healthcheck.WORKER_STALE_SECONDS + 10)
    beat_file.write_text(str(stale))
    result = _run(_worker_source(beat_file))
    assert result.returncode == 1, "a beat older than the threshold must be unhealthy"
    assert "old" in result.stdout, f"the failure line must say the beat is stale; {result.stdout!r}"


def test_inline_worker_probe_exits_zero_when_the_beacon_was_never_written(tmp_path: Path) -> None:
    """The documented divergence from `_check_worker`, executed rather than grepped.

    `test_worker_inline_copy_passes_when_the_beacon_was_never_written` asserts the
    `sys.exit(0)` guard is textually first. This runs the probe with no file at
    all and checks the guard actually takes that branch — the behaviour AUD-544
    was reopened for.
    """
    _skip_if_module_form(_worker_healthcheck_test(), WORKER_MODULE_FORM)
    beat_file = tmp_path / "never-written"
    assert not beat_file.exists()
    result = _run(_worker_source(beat_file))
    assert result.returncode == 0, (
        "a beacon file that was never written means the image predates the writer, not "
        f"a dead worker (AUD-544); stdout={result.stdout!r}"
    )


def test_inline_worker_probe_prints_one_line_on_an_unreadable_beat(tmp_path: Path) -> None:
    _skip_if_module_form(_worker_healthcheck_test(), WORKER_MODULE_FORM)
    beat_file = tmp_path / "worker-heartbeat"
    beat_file.write_text("not a timestamp")
    result = _run(_worker_source(beat_file))
    assert result.returncode == 1, "an unparseable beat must be unhealthy"
    assert "Traceback" not in result.stderr, (
        f"the probe must catch and print, never raise; stderr={result.stderr!r}"
    )
    assert len(result.stdout.strip().splitlines()) == 1, (
        f"expected exactly one printed line, got {result.stdout!r}"
    )
