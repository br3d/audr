"""Static guards on the Alembic revision chain (AUD-698).

The deploy's rollback path is built on `alembic downgrade` working. On a failed
health check the guarded deploy downgrades the schema and restores the previous
image; when the downgrade fails it has nothing left to try, so it reports
'downgrade failed' and *leaves the stack on the new build* — the one outcome the
rollback exists to prevent (ci/gitea-overlay/workflows/deploy.yaml, "Guarded
deploy"). An irreversible or multi-headed migration is therefore a one-way door,
and nothing used to flag it before it was merged.

This file is the half of that guard which needs no database: it reads the
revision graph and the revision *source* and fails on the shapes that break the
rollback. The other half — actually running upgrade -> downgrade -> upgrade
against Postgres — is scripts/migration-smoke.sh, run as the `migration-smoke`
compose service and as its own test.sh phase. The split is deliberate: a second
head and an empty `downgrade()` are both decidable from the files alone, so they
should be decidable in milliseconds and without an engine, which is what makes
them cheap enough to run on every push. Only the things Postgres alone can
answer — a constraint restored under data that violates it, a column some later
index needs — justify spending a database.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.unit

# backend/tests/unit/test_migration_chain.py -> backend/
_BACKEND = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND / "alembic.ini"
_VERSIONS = _BACKEND / "migrations" / "versions"

# Revisions that are knowingly, permanently irreversible, with the reason.
#
# This is an allowlist, not a waiver list: a revision belongs here only when the
# change genuinely cannot be reversed, which in practice means it destroyed or
# rewrote data rather than altering a schema. Every entry is a revision whose
# `downgrade()` is empty or a no-op ON PURPOSE, and the cost of that choice is
# real — a deploy that fails its health check after one of these has run cannot
# be rolled back by the workflow and needs the manual path in
# docs/deploy-runbook.md.
#
# Adding to this list is a decision about the rollback story of a production
# deploy. Make it explicitly, and write the reason: the person who needs it is
# reading a failed deploy at the time.
IRREVERSIBLE: dict[str, str] = {
    "0007": (
        "Rewrote string column server defaults in place. The pre-0007 defaults "
        "were the bug, and no record of the old values survives the UPDATE, so "
        "there is nothing to put back."
    ),
    "0020": (
        "Data repair: recomputed history_point totals that were stored wrong. "
        "Restoring the wrong totals is not a rollback anybody wants."
    ),
    "0027": (
        "Data purge: deleted asset_news rows attributed to the wrong asset. The "
        "rows are gone; re-ingestion is the forward fix, not a downgrade."
    ),
}


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(_ALEMBIC_INI))
    # alembic.ini resolves `script_location = migrations` and
    # `version_locations = %(here)s/migrations/versions` relative to its own
    # directory, and `here` is only set when alembic is invoked from there.
    # Point both at absolute paths so the graph loads whatever the cwd is —
    # pytest runs from /workspace/backend, the deploy from /app.
    cfg.set_main_option("script_location", str(_BACKEND / "migrations"))
    cfg.set_main_option("version_locations", str(_VERSIONS))
    return ScriptDirectory.from_config(cfg)


def test_the_chain_has_exactly_one_head() -> None:
    """Two heads and the next deploy cannot migrate at all.

    `alembic upgrade head` refuses to run against a branched chain
    ("Multiple head revisions are present"), so this is not a style point: it
    stops the deploy outright, after the image has already been built and
    pushed. The normal way to get here is merging two branches that each added a
    migration — each file is correct, it is the pair that is wrong, and that is
    exactly what a reviewer looking at one diff cannot see.
    """
    heads = _script_directory().get_heads()
    assert len(heads) == 1, (
        f"the revision chain has {len(heads)} heads: {sorted(heads)}.\n"
        "Re-point one revision's down_revision at the other so the chain is "
        "linear again. Do NOT use `alembic merge`: the deploy's rollback walks "
        "the chain with `downgrade -1`, which has no defined meaning at a merge "
        "point."
    )


def test_the_chain_is_linear() -> None:
    """No revision may have two children, even transiently.

    A single head is necessary but not sufficient: a branch that forks and is
    later joined by a merge revision reports one head while still containing a
    point where `downgrade -1` is ambiguous. The rollback walks backwards one
    step at a time, so ambiguity there is ambiguity in the recovery path.
    """
    script = _script_directory()
    children: dict[str, list[str]] = {}
    for revision in script.walk_revisions():
        # down_revision is a str for a normal revision and a tuple for a merge
        # revision; iterating the str case directly would yield characters.
        down = revision.down_revision
        parents = (down,) if isinstance(down, str) else tuple(down or ())
        for parent in parents:
            children.setdefault(parent, []).append(revision.revision)

    forks = {parent: kids for parent, kids in children.items() if len(kids) > 1}
    assert not forks, (
        f"the revision chain forks: {forks}.\n"
        "Each listed revision has more than one child, so `downgrade -1` from "
        "below the fork has no single answer. Rebase the branches into one line."
    )


def _downgrade_body(path: Path) -> list[ast.stmt]:
    """The statements inside `def downgrade()`, docstring and comments removed.

    Parsed rather than grepped: `pass`, an empty body and a body that is nothing
    but a docstring are three different source shapes and one semantic fact, and
    a regex over the text gets at least one of them wrong.
    """
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = list(node.body)
            # Drop a leading docstring: it is documentation, not a statement
            # that reverses anything.
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return [stmt for stmt in body if not isinstance(stmt, ast.Pass)]
    raise AssertionError(f"{path.name} has no `downgrade()` at module level")


def test_every_revision_defines_a_downgrade() -> None:
    """A revision with no `downgrade()` at all breaks the rollback at import.

    Distinct from an empty one: alembic raises `NotImplementedError` when the
    attribute is missing, and that happens *during* the rollback, after the
    health check has already failed.
    """
    missing = []
    for path in sorted(_VERSIONS.glob("[0-9]*.py")):
        try:
            _downgrade_body(path)
        except AssertionError:
            missing.append(path.name)
    assert not missing, f"revisions with no module-level `downgrade()`: {missing}"


def test_no_new_revision_is_silently_irreversible() -> None:
    """An empty `downgrade()` must be a decision, not an omission.

    This is the guard the ticket was opened for. An empty downgrade passes
    review easily — it is the absence of code — and its cost lands much later,
    on whoever is watching a failed deploy decline to roll back. Requiring the
    revision to be named in IRREVERSIBLE above does not prevent anything; it
    makes the choice visible at the moment it is made, and records the reason
    where the next reader will need it.
    """
    silent = []
    for path in sorted(_VERSIONS.glob("[0-9]*.py")):
        revision = path.name.split("_", 1)[0]
        if _downgrade_body(path):
            continue
        if revision not in IRREVERSIBLE:
            silent.append(path.name)

    assert not silent, (
        f"these revisions have an empty `downgrade()` and are not declared "
        f"irreversible: {silent}.\n\n"
        "The deploy's rollback runs `alembic downgrade` on a failed health "
        "check and gives up when it fails, leaving the stack on the new build. "
        "So either write a downgrade that really reverses the change, or add "
        "the revision id to IRREVERSIBLE in this file with the reason it cannot "
        "be reversed."
    )


def test_the_irreversible_allowlist_has_no_stale_entries() -> None:
    """An allowlist nobody prunes stops describing the tree.

    Two ways an entry goes stale: the revision file is deleted (a squash to a
    new baseline, as AUD-306 did), or somebody later writes it a real downgrade.
    Either way the entry now excuses nothing, and the next reader takes it as
    evidence that revision cannot be rolled back when it can.
    """
    present = {path.name.split("_", 1)[0] for path in _VERSIONS.glob("[0-9]*.py")}
    gone = sorted(set(IRREVERSIBLE) - present)
    assert not gone, (
        f"IRREVERSIBLE names revisions that no longer exist: {gone}. "
        "Drop them — most likely a squash-to-baseline removed the files."
    )

    now_reversible = []
    for revision in sorted(IRREVERSIBLE):
        matches = list(_VERSIONS.glob(f"{revision}_*.py"))
        if matches and _downgrade_body(matches[0]):
            now_reversible.append(revision)
    assert not now_reversible, (
        f"these revisions are on the irreversible allowlist but now have a real "
        f"`downgrade()`: {now_reversible}. Remove them from IRREVERSIBLE so the "
        "list keeps meaning what it says."
    )


def test_every_revision_id_is_unique() -> None:
    """Two files claiming one revision id makes the graph silently wrong.

    alembic resolves the duplicate to whichever file it loaded last, so one of
    the two migrations simply never runs — and `alembic current` reports the
    shared id either way, so the schema and the version table disagree with no
    error anywhere.
    """
    seen: dict[str, list[str]] = {}
    for path in sorted(_VERSIONS.glob("[0-9]*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            targets = (
                [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
            )
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if "revision" in names and isinstance(node.value, ast.Constant):
                seen.setdefault(str(node.value.value), []).append(path.name)

    duplicates = {rev: files for rev, files in seen.items() if len(files) > 1}
    assert not duplicates, f"revision ids declared by more than one file: {duplicates}"
