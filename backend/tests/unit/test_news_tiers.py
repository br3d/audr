"""Unit tests for the shared capped/rate-limited tier runner (AUD-691)."""

from __future__ import annotations

import uuid

import pytest

from audr.jobs.news_tiers import NewsTier, run_capped_tier

pytestmark = pytest.mark.unit

_RUN_ID = uuid.uuid4()


def _tier(**overrides: object) -> NewsTier:
    base = {
        "name": "test-tier",
        "max_queries_per_run": 10,
        "max_consecutive_429": 3,
        "query_delay_s": 0,
    }
    base.update(overrides)
    return NewsTier(**base)


async def test_caps_items_to_max_queries_per_run() -> None:
    calls: list[int] = []

    async def poll_one(item: int) -> tuple[int, bool]:
        calls.append(item)
        return 1, False

    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1

    inserted, polled = await run_capped_tier(
        run_id=_RUN_ID,
        tier=_tier(max_queries_per_run=3),
        items=list(range(10)),
        poll_one=poll_one,
        commit=commit,
    )

    assert calls == [0, 1, 2]
    assert inserted == 3
    assert polled == 3
    assert commits == 3


async def test_aborts_after_consecutive_429s_and_commits_by_default() -> None:
    calls: list[int] = []

    async def poll_one(item: int) -> tuple[int, bool]:
        calls.append(item)
        return 0, True

    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1

    inserted, polled = await run_capped_tier(
        run_id=_RUN_ID,
        tier=_tier(max_consecutive_429=3),
        items=list(range(10)),
        poll_one=poll_one,
        commit=commit,
    )

    assert calls == [0, 1, 2]
    assert inserted == 0
    assert polled == 0
    assert commits == 3


async def test_a_success_resets_the_consecutive_429_streak() -> None:
    results = [(0, True), (0, True), (1, False), (0, True), (0, True), (0, True)]
    calls = iter(results)

    async def poll_one(_item: int) -> tuple[int, bool]:
        return next(calls)

    async def commit() -> None:
        return None

    inserted, polled = await run_capped_tier(
        run_id=_RUN_ID,
        tier=_tier(max_consecutive_429=3),
        items=list(range(len(results))),
        poll_one=poll_one,
        commit=commit,
    )

    assert inserted == 1
    assert polled == 1


async def test_commit_on_rate_limit_false_skips_commit_on_429() -> None:
    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1

    async def poll_one(item: int) -> tuple[int, bool]:
        return (0, True) if item < 2 else (1, False)

    inserted, polled = await run_capped_tier(
        run_id=_RUN_ID,
        tier=_tier(max_consecutive_429=5),
        items=[0, 1, 2],
        poll_one=poll_one,
        commit=commit,
        commit_on_rate_limit=False,
    )

    assert inserted == 1
    assert polled == 1
    # Only the one successful item commits — the two rate-limited ones don't.
    assert commits == 1


async def test_commit_on_rate_limit_false_also_skips_commit_on_abort() -> None:
    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1

    async def poll_one(_item: int) -> tuple[int, bool]:
        return 0, True

    await run_capped_tier(
        run_id=_RUN_ID,
        tier=_tier(max_consecutive_429=2),
        items=[0, 1, 2, 3],
        poll_one=poll_one,
        commit=commit,
        commit_on_rate_limit=False,
    )

    assert commits == 0
