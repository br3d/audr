#!/usr/bin/env python3
"""Unit tests for scripts/registry-prune.py.

Deliberately dependency-free: run it with `python3 scripts/test_registry_prune.py`.
The backend pytest suite is not the right home for this — it runs inside a
container that only copies `backend/`, so it cannot see `scripts/`.

Every case here is a retention rule whose failure mode is a disarmed rollback,
not a cosmetic mistake.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys

_SCRIPT = pathlib.Path(__file__).with_name("registry-prune.py")
_spec = importlib.util.spec_from_file_location("registry_prune", _SCRIPT)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

ALIASES = set(rp.ALIAS_TAGS)


def survey(*rows):
    """Build the structure survey_repo() returns, from (tag, created, digest)."""
    return {
        "tags": [r[0] for r in rows],
        "created": {r[0]: r[1] for r in rows},
        "digests": {r[0]: r[2] for r in rows},
    }


def reasons(survey_, protected, keep_set, keep):
    return {tag: why for tag, _, _, why in rp.plan_repo(survey_, protected, keep_set, keep)[0]}


def prune_set(survey_, protected, keep_set, keep):
    return {tag for tag, why in reasons(survey_, protected, keep_set, keep).items() if why is None}


def test_aliases_and_release_tags_are_never_pruned():
    s = survey(
        ("latest", "2026-01-01", "sha256:a"),
        ("rollback", "2026-01-01", "sha256:b"),
        ("v0.1.0", "2026-01-01", "sha256:c"),
    )
    assert prune_set(s, ALIASES, set(), 0) == set()


def test_release_tag_pattern():
    for tag in ("v0.1.0", "v1", "v0.1.0-rc1", "v10.2.3"):
        assert rp.RELEASE_TAG_RE.match(tag), tag
    for tag in ("version", "vabc", "0.1.0", "latest"):
        assert not rp.RELEASE_TAG_RE.match(tag), tag


def test_digest_shared_with_protected_tag_is_kept():
    """A digest delete removes EVERY tag pointing at it.

    `stale` is the oldest tag, so newest-N cannot save it — only the fact that it
    resolves to the same digest as `latest`. Pruning it would delete the image
    `latest` names.
    """
    s = survey(
        ("latest", "2026-01-03", "sha256:shared"),
        ("stale", "2026-01-01", "sha256:shared"),
        ("other", "2026-01-02", "sha256:other"),
    )
    keep_set = rp.newest_tags(s, 1, ALIASES)
    assert keep_set == {"other"}
    why = reasons(s, ALIASES, keep_set, 1)["stale"]
    assert why and "shares a digest" in why, why


def test_keep_set_is_symmetric_across_repositories():
    """When more than one repository is pruned, a sha is kept in all or none.

    AUD-388 reduced DEFAULT_REPOS to the single `audr-backend`, so this no
    longer guards a live deploy invariant — but the union keep-set it exercises
    is still the mechanism any `--repo a --repo b` run depends on, and it is
    cheaper to keep the test than to rediscover why the union exists.

    Originally written for the two-image era, where a deploy pinned
    BACKEND_TAG and FRONTEND_TAG to the same sha and rollback pulled that sha
    from both repositories, so an asymmetric keep-set yielded a rollback that
    half-succeeded. The disagreement it models was not hypothetical: the same
    sha carried different image `created` dates in each repository (observed
    live — 8d52a8f036dc was 20:51 in audr-backend and 17:34 in audr-frontend),
    so per-repo newest-N genuinely disagrees.
    """
    repo_a = survey(
        ("shaA", "2026-01-02T20:51", "sha256:1"), ("shaB", "2026-01-02T17:34", "sha256:2")
    )
    repo_b = survey(
        ("shaA", "2026-01-02T17:34", "sha256:3"), ("shaB", "2026-01-02T18:00", "sha256:4")
    )

    per_repo_a = rp.newest_tags(repo_a, 1, ALIASES)
    per_repo_b = rp.newest_tags(repo_b, 1, ALIASES)
    # Guard the premise: if these ever agree, this test has stopped testing it.
    assert per_repo_a != per_repo_b

    # Ranking each repository on its own would prune, in each, the sha the other keeps.
    assert set(repo_a["tags"]) - per_repo_a != set(repo_b["tags"]) - per_repo_b

    union = per_repo_a | per_repo_b
    assert prune_set(repo_a, ALIASES, union, 1) == set()
    assert prune_set(repo_b, ALIASES, union, 1) == set()


def test_ties_break_deterministically():
    s = survey(("x", "2026-01-02", "sha256:1"), ("y", "2026-01-02", "sha256:2"))
    assert rp.newest_tags(s, 1, ALIASES) == rp.newest_tags(s, 1, ALIASES) == {"y"}


def test_unknown_creation_time_is_not_pruned_on_age():
    s = survey(("latest", "2026-01-05", "sha256:l"), ("nodate", None, "sha256:n"))
    why = reasons(s, ALIASES, rp.newest_tags(s, 1, ALIASES), 1)["nodate"]
    assert why == "creation time unknown"


def test_unresolvable_digest_is_not_pruned():
    s = survey(("latest", "2026-01-05", "sha256:l"), ("baddigest", "2026-01-01", None))
    assert reasons(s, ALIASES, set(), 1)["baddigest"] == "digest unresolvable"


def test_deployed_tag_is_protected():
    s = survey(("latest", "2026-01-05", "sha256:l"), ("live", "2026-01-01", "sha256:v"))
    assert reasons(s, ALIASES | {"live"}, set(), 1)["live"] == "deployed/explicitly protected"


def test_newest_tags_respects_keep_count():
    s = survey(
        ("t1", "2026-01-01", "sha256:1"),
        ("t2", "2026-01-02", "sha256:2"),
        ("t3", "2026-01-03", "sha256:3"),
    )
    assert rp.newest_tags(s, 2, ALIASES) == {"t3", "t2"}


def test_prunable_groups_tags_by_digest():
    """Two tags on one digest must be one delete, reported as both tags."""
    s = survey(
        ("keep", "2026-01-09", "sha256:keep"),
        ("dupA", "2026-01-01", "sha256:dup"),
        ("dupB", "2026-01-01", "sha256:dup"),
    )
    _, prunable = rp.plan_repo(s, ALIASES, {"keep"}, 1)
    assert list(prunable) == ["sha256:dup"]
    assert sorted(prunable["sha256:dup"]) == ["dupA", "dupB"]


def expect_argparse_error(argv):
    """argparse exits 2 and prints usage; swallow the usage text so that a real
    failure is the only thing this suite prints."""
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            rp.main(argv)
        except SystemExit as exc:
            assert exc.code == 2, (argv, exc.code)
            return
    raise AssertionError(f"{argv} was not rejected")


def test_repo_allowlist_rejects_foreign_repositories():
    """The registry is shared with the unrelated svetu-* project."""
    for repo in ("svetu-backend", "library/aud-pushtest", "postgres"):
        expect_argparse_error(["--repo", repo])


def test_keep_must_be_positive():
    expect_argparse_error(["--keep", "0"])


def test_read_env_tags():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / ".env"
        path.write_text(
            "DB_PASSWORD=secret\n"
            "BACKEND_TAG=2f1a4c5b2d99\n"
            # A stale line left in the live .env from before AUD-388 dropped the
            # second image. Deploys no longer write it and it must not protect
            # the old sha it names — hence a different value to the one above.
            "FRONTEND_TAG=aaaa1111bbbb\n"
            "# note\n"
            "EMPTY=\n",
            encoding="utf-8",
        )
        assert rp.read_env_tags(str(path)) == {"2f1a4c5b2d99"}
    # A missing file returns None — distinct from "read it, found nothing" — so
    # main() can warn that the live tag is unprotected instead of assuming it is.
    assert rp.read_env_tags(str(pathlib.Path(d) / "gone")) is None


def main():
    tests = [
        (n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test runner
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
