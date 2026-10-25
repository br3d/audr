"""The CI phase-ledger replay, scripts/ci-phase-ledger.sh (AUD-715).

Same shape as tests/unit/test_alert_dispatch.py: the real shipped script runs
against a synthetic ledger, so what is asserted is the script and not a
reimplementation of it.

WHAT IS WORTH TESTING. Not the step log text; it is printf. This script's whole
purpose is that an agent with no credentials can read *which suite phase failed*
off GitHub step metadata — the artifact download endpoint needs authentication
and the job log needs repository admin (AUD-714). So the failure modes that cost
something are the ones that corrupt that signal:

  * a failed phase reported as `pass` — the one thing this exists to prevent;
    the job stays red for the real reason but the metadata names the wrong
    phase, which is strictly worse than no metadata at all;
  * a phase the run never reached reported as `pass` rather than `skip` — makes
    "the frontend leg died before it started" indistinguishable from "the
    frontend leg passed", which is the distinction AUD-714 was blind to;
  * an output missing entirely for a known phase — reads as the empty string in
    a workflow `if:`, which is neither 'pass' nor 'skip', so the verdict step
    silently runs with no state and the phase vanishes from the job;
  * a drifted phase list — a slug test.sh records that this script does not know
    is a phase no CI reader can see, and must be warned about loudly;
  * aborting on a malformed ledger: this script runs with `if: always()` on a
    job that may already be red for the real reason, so a truncated row must
    degrade, never raise.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
LEDGER_SCRIPT = REPO_ROOT / "scripts" / "ci-phase-ledger.sh"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
TEST_SH = REPO_ROOT / "scripts" / "test.sh"

# Every phase the script must report on, in the order a reader sees them.
# `ruff` and `backend_mypy` are the static gates added in AUD-744; they report
# `skip` in the GitHub `tests` job, which sets AUDR_SKIP_STATIC=1.
# `image_pull` (AUD-747) is first: the outbound pulls were moved out of the
# phases that merely needed an image, so a rate-limited registry stops reading
# as a failure of the tree.
PHASES = (
    "image_pull",
    "ruff",
    "db_up",
    "backend_build",
    "migration_smoke",
    "backend_suite",
    "backend_mypy",
    "frontend_install",
    "frontend_suite",
    "api_types",
)


def run_ledger(tmp_path: Path, rows: str | None) -> tuple[dict[str, str], str, int]:
    """Run the real script over a synthetic ledger; return its step outputs.

    `rows` of None means no ledger file at all — the "died before test.sh" case.
    """
    ledger = tmp_path / ".test-phases.tsv"
    if rows is not None:
        ledger.write_text(rows)
    out_file = tmp_path / "github_output"
    out_file.touch()

    # S603/S607 as in test_alert_dispatch.py: the argv is the tracked script
    # itself plus a ledger under tmp_path, and `bash` resolves from PATH exactly
    # as the workflow step resolves it.
    proc = subprocess.run(  # noqa: S603 — argv is a tracked script and a tmp_path fixture
        ["bash", str(LEDGER_SCRIPT), str(ledger)],  # noqa: S607 — `bash` from PATH, as CI runs it
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_OUTPUT": str(out_file)},
        cwd=tmp_path,
        timeout=120,
    )
    outputs: dict[str, str] = {}
    for line in out_file.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return outputs, proc.stdout + proc.stderr, proc.returncode


def row(slug: str, rc: int, start: int = 1000, end: int = 1007) -> str:
    return f"{slug}\t{rc}\t{start}\t{end}\tsome label\n"


def test_green_run_reports_every_phase_pass(tmp_path: Path) -> None:
    outputs, _, rc = run_ledger(tmp_path, "".join(row(p, 0) for p in PHASES))

    assert rc == 0
    assert [outputs[p] for p in PHASES] == ["pass"] * len(PHASES)


def test_failed_phase_is_the_only_one_marked_fail(tmp_path: Path) -> None:
    """The core contract: the metadata must name the phase that actually died."""
    rows = row("db_up", 0) + row("backend_build", 0) + row("backend_suite", 1)
    outputs, _, rc = run_ledger(tmp_path, rows)

    assert rc == 0, "the replay itself must not fail, it only reports"
    assert outputs["backend_suite"] == "fail"
    assert outputs["db_up"] == "pass"
    assert outputs["backend_build"] == "pass"
    # Everything after the death was never reached, and must not read as passing.
    for later in ("frontend_install", "frontend_suite", "api_types"):
        assert outputs[later] == "skip"


def test_every_known_phase_gets_an_output_even_when_unreached(tmp_path: Path) -> None:
    """A missing output is the empty string in a workflow `if:`, not 'skip'."""
    outputs, _, _ = run_ledger(tmp_path, row("db_up", 1))

    for phase in PHASES:
        assert phase in outputs, f"{phase} has no state output"
        assert f"{phase}_detail" in outputs, f"{phase} has no detail output"
        assert outputs[phase] in {"pass", "fail", "skip"}


def test_absent_ledger_skips_everything_rather_than_passing(tmp_path: Path) -> None:
    """No ledger means the run died before test.sh ran — never a green verdict."""
    outputs, log, rc = run_ledger(tmp_path, None)

    assert rc == 0
    assert [outputs[p] for p in PHASES] == ["skip"] * len(PHASES)
    assert "died before" in log


def test_nonzero_rc_other_than_one_still_fails(tmp_path: Path) -> None:
    """pytest's own exit codes are 1-5, and `up --wait` timing out exits 1-ish."""
    outputs, _, _ = run_ledger(tmp_path, row("backend_suite", 137))

    assert outputs["backend_suite"] == "fail"
    assert "rc=137" in outputs["backend_suite_detail"]


def test_detail_carries_the_phase_duration(tmp_path: Path) -> None:
    outputs, _, _ = run_ledger(tmp_path, row("backend_suite", 0, start=100, end=312))

    assert "212s" in outputs["backend_suite_detail"]


def test_truncated_row_degrades_instead_of_aborting(tmp_path: Path) -> None:
    """`if: always()` means a malformed ledger must not mask the real failure."""
    outputs, _, rc = run_ledger(tmp_path, "backend_suite\t1\t\t\n" + row("db_up", 0))

    assert rc == 0
    assert outputs["backend_suite"] == "fail"
    assert outputs["db_up"] == "pass"


def test_unknown_phase_slug_warns_loudly(tmp_path: Path) -> None:
    """A slug test.sh records and this script does not know is invisible in CI."""
    outputs, log, rc = run_ledger(tmp_path, row("db_up", 0) + row("e2e_suite", 0))

    assert rc == 0, "a stale phase list must not become a second, unrelated red"
    assert "e2e_suite" in log
    assert "WARNING" in log
    assert outputs["db_up"] == "pass"


def test_phase_slugs_match_the_ones_test_sh_records() -> None:
    """The two files drift silently otherwise: this is the only thing that pairs them.

    `record_phase` in test.sh and `PHASES` in ci-phase-ledger.sh are the same
    list written twice. A phase recorded but not replayed is invisible in CI; a
    phase replayed but never recorded shows as permanently `skip`.
    """
    recorded = {
        line.split("record_phase", 1)[1].split()[0]
        for line in TEST_SH.read_text().splitlines()
        if "record_phase " in line and not line.lstrip().startswith("#")
    }
    # The `run_phase` wrapper calls record_phase with "${slug}", so pull the
    # slugs its own callers pass instead.
    recorded.discard('"${slug}"')
    recorded |= {
        line.split("run_phase", 1)[1].split()[0]
        for line in TEST_SH.read_text().splitlines()
        if "run_phase " in line and not line.lstrip().startswith("#") and "run_phase()" not in line
    }

    assert recorded == set(PHASES), (
        "scripts/test.sh and scripts/ci-phase-ledger.sh disagree on the phase list; "
        f"only in test.sh: {sorted(recorded - set(PHASES))}, "
        f"only in the replay: {sorted(set(PHASES) - recorded)}"
    )


def test_every_phase_has_a_verdict_step_in_the_tests_workflow() -> None:
    """The phase list is written a third time, in .github/workflows/tests.yml.

    A phase with no verdict step is recorded, replayed into a step output, and
    then read by nobody — the exact invisibility AUD-715 exists to remove, with
    none of the warnings the ledger raises for a slug it does not know. The
    `steps.phases.outputs.<slug>` reference is what pairs them.
    """
    workflow = TESTS_WORKFLOW.read_text()
    missing = [p for p in PHASES if f"steps.phases.outputs.{p} " not in workflow]

    assert not missing, (
        f".github/workflows/tests.yml has no verdict step for: {missing}; "
        "every phase in PHASES needs one or its result is unreadable in CI"
    )


def test_tests_job_skips_the_static_gates_it_runs_as_separate_jobs() -> None:
    """AUD-744: the static gates are for the local run, not for duplication here.

    `lint` and `backend-mypy` are their own jobs in this workflow, on their own
    runners, in parallel with `tests`. If the gate step ever stops setting
    AUDR_SKIP_STATIC the suite job silently grows a redundant ruff and mypy run
    on the critical path — green, slower, and proving nothing new.
    """
    workflow = TESTS_WORKFLOW.read_text()

    assert "AUDR_SKIP_STATIC: '1'" in workflow, (
        "the `tests` job no longer sets AUDR_SKIP_STATIC=1, so scripts/test.sh "
        "will re-run ruff and mypy inside it; the `lint` and `backend-mypy` "
        "jobs already cover both"
    )
    for job in ("  lint:", "  backend-mypy:"):
        assert job in workflow, (
            f"{job.strip()} is gone from tests.yml, but the `tests` job still "
            "sets AUDR_SKIP_STATIC=1 — that combination enforces neither gate "
            "anywhere in CI"
        )
