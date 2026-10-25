"""Unit tests for the news-analysis gate decision logic (AUD-658).

Acceptance criteria this exercises directly: significant / not significant /
LLM error -> fail-open / run budget exhausted -> fail-open, all on a stubbed
client so no network or database is involved.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from audr.jobs import news_gate as mod
from audr.jobs.news_gate import NewsAnalysisGate, _RunBudget
from audr.providers.llm import ClassificationResult, LlmTimeout

pytestmark = pytest.mark.unit

_ASSET_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _no_real_full_text_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """These are decision-logic unit tests, not a trafilatura contract test
    — stub the one place this module touches the network so they stay fast
    and deterministic regardless of outbound network access.
    """
    monkeypatch.setattr(mod, "_fetch_full_text", AsyncMock(return_value=""))


@dataclass
class _FakeClient:
    result: ClassificationResult | None = None
    error: Exception | None = None
    calls: int = 0

    async def classify_article(self, **_kwargs: object) -> ClassificationResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    async def close(self) -> None:
        pass


def _session_mock(*, already_handled: bool = False) -> MagicMock:
    """A session whose `_already_handled` SELECTs report nothing found,
    unless *already_handled* makes the first one (asset_news) match.
    """
    session = MagicMock()
    first_result = MagicMock()
    first_result.first.return_value = (1,) if already_handled else None
    session.execute = AsyncMock(return_value=first_result)
    return session


def _ingest_kwargs(insert: AsyncMock) -> dict:
    return {
        "asset_id": _ASSET_ID,
        "source": "project:eth",
        "external_id": "abc123",
        "title": "Ethereum update",
        "url": "https://example.com/a",
        "news_site": "example.com",
        "published_at": datetime(2026, 10, 24, tzinfo=UTC),
        "asset_symbol": "ETH",
        "asset_name": "Ethereum",
        "insert": insert,
    }


class TestGateDisabled:
    async def test_passthrough_inserts_without_calling_llm(self) -> None:
        client = _FakeClient()
        gate = NewsAnalysisGate(
            enabled=False,
            threshold=50,
            client=client,
            budget=_RunBudget(Decimal(0)),
            run_id=_RUN_ID,
        )
        session = _session_mock()
        insert = AsyncMock(return_value=1)

        rows = await gate.ingest(session, **_ingest_kwargs(insert))

        assert rows == 1
        assert client.calls == 0
        insert.assert_awaited_once()
        assert "summary" not in insert.await_args.kwargs
        assert "importance" not in insert.await_args.kwargs


class TestGateEnabledSignificant:
    async def test_important_article_is_inserted_with_summary_and_importance(self) -> None:
        client = _FakeClient(
            result=ClassificationResult(
                important=True,
                importance=80,
                summary="Big news.",
                reason="hack",
                cost_usd=Decimal("0.001"),
            )
        )
        gate = NewsAnalysisGate(
            enabled=True,
            threshold=50,
            client=client,
            budget=_RunBudget(Decimal("1.00")),
            run_id=_RUN_ID,
        )
        session = _session_mock()
        insert = AsyncMock(return_value=1)

        rows = await gate.ingest(session, **_ingest_kwargs(insert))

        assert rows == 1
        insert.assert_awaited_once()
        assert insert.await_args.kwargs["summary"] == "Big news."
        assert insert.await_args.kwargs["importance"] == 80


class TestGateEnabledUnimportant:
    async def test_unimportant_article_is_suppressed_not_inserted(self) -> None:
        client = _FakeClient(
            result=ClassificationResult(
                important=False,
                importance=10,
                summary="",
                reason="routine listicle",
                cost_usd=Decimal("0.001"),
            )
        )
        gate = NewsAnalysisGate(
            enabled=True,
            threshold=50,
            client=client,
            budget=_RunBudget(Decimal("1.00")),
            run_id=_RUN_ID,
        )
        session = _session_mock()
        insert = AsyncMock(return_value=1)

        rows = await gate.ingest(session, **_ingest_kwargs(insert))

        assert rows == 0
        insert.assert_not_awaited()
        # The suppressed-row INSERT is the one `execute` call beyond the two
        # `_already_handled` SELECTs.
        assert session.execute.await_count == 3


class TestGateFailsOpen:
    async def test_llm_error_falls_back_to_plain_insert(self) -> None:
        client = _FakeClient(error=LlmTimeout("boom"))
        gate = NewsAnalysisGate(
            enabled=True,
            threshold=50,
            client=client,
            budget=_RunBudget(Decimal("1.00")),
            run_id=_RUN_ID,
        )
        session = _session_mock()
        insert = AsyncMock(return_value=1)

        rows = await gate.ingest(session, **_ingest_kwargs(insert))

        assert rows == 1
        insert.assert_awaited_once()
        assert "summary" not in insert.await_args.kwargs

    async def test_no_client_configured_falls_back_to_plain_insert(self) -> None:
        """Gate on, no Anthropic key stored — fail-open for every article."""
        gate = NewsAnalysisGate(
            enabled=True,
            threshold=50,
            client=None,
            budget=_RunBudget(Decimal("1.00")),
            run_id=_RUN_ID,
        )
        session = _session_mock()
        insert = AsyncMock(return_value=1)

        rows = await gate.ingest(session, **_ingest_kwargs(insert))

        assert rows == 1
        insert.assert_awaited_once()

    async def test_budget_exhausted_falls_back_to_plain_insert_without_calling_llm(self) -> None:
        """A zero-room budget skips the LLM call entirely rather than
        spending past the run's ceiling (AUD-658 acceptance criterion).
        """
        client = _FakeClient(
            result=ClassificationResult(
                important=False, importance=0, summary="", reason="", cost_usd=Decimal("0.001")
            )
        )
        exhausted_budget = _RunBudget(Decimal("1.00"), spent_usd=Decimal("1.00"))
        gate = NewsAnalysisGate(
            enabled=True, threshold=50, client=client, budget=exhausted_budget, run_id=_RUN_ID
        )
        session = _session_mock()
        insert = AsyncMock(return_value=1)

        rows = await gate.ingest(session, **_ingest_kwargs(insert))

        assert rows == 1
        assert client.calls == 0
        insert.assert_awaited_once()


class TestAlreadyHandled:
    async def test_already_in_asset_news_skips_without_calling_llm(self) -> None:
        client = _FakeClient()
        gate = NewsAnalysisGate(
            enabled=True,
            threshold=50,
            client=client,
            budget=_RunBudget(Decimal("1.00")),
            run_id=_RUN_ID,
        )
        session = _session_mock(already_handled=True)
        insert = AsyncMock(return_value=1)

        rows = await gate.ingest(session, **_ingest_kwargs(insert))

        assert rows == 0
        assert client.calls == 0
        insert.assert_not_awaited()
