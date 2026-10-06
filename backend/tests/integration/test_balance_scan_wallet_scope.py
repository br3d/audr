"""handle_balance_scan / handle_discovery per-wallet scope (AUD-399/AUD-400).

A `job_run.params = {"wallet_id": ...}` row must restrict the worker to that
one wallet — no RPC calls (balance_scan) or discovery pass for any other
tracked wallet. This is the whole point of the feature: refreshing one
wallet must not burn RPC quota scanning every other tracked address. A run
with no params must still behave exactly as before (all active wallets).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.jobs import __main__ as worker_main
from audr.jobs.store import JobKind, enqueue_job
from audr.wallets.service import add_wallet, stop_wallet

_WALLET_A = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
_WALLET_B = "0x00000000219ab540356cbb839cbe05303d7705fa"

_CLEAN_ORDER = (
    "balance_observation",
    "discovery_coverage",
    "monitored_pair",
    "job_run",
    "wallet",
    "asset",
)


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            for tbl in _CLEAN_ORDER:
                await session.execute(text(f"DELETE FROM {tbl}"))  # noqa: S608 — tbl comes from the hardcoded _CLEAN_ORDER tuple above, not user input


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


class _FakeRpcReader:
    """Records every address it is asked to read a balance for."""

    def __init__(self, **_kwargs: Any) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __aenter__(self) -> _FakeRpcReader:
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False

    async def validate_chain(self) -> None:
        return None

    async def get_block_number(self) -> int:
        return 100

    async def get_block_time(self, _block_number: int) -> None:
        return None

    async def get_eth_balance(self, address: str) -> int:
        self.calls.append(("eth", address))
        return 0

    async def get_erc20_balance(self, *, token_address: str, wallet_address: str) -> int:
        self.calls.append(("erc20", wallet_address, token_address))
        return 0


@pytest.fixture()
def fake_rpc_readers(monkeypatch: pytest.MonkeyPatch) -> list[_FakeRpcReader]:
    instances: list[_FakeRpcReader] = []

    def _make_fake(**kwargs: Any) -> _FakeRpcReader:
        reader = _FakeRpcReader(**kwargs)
        instances.append(reader)
        return reader

    monkeypatch.setattr(worker_main, "RpcReader", _make_fake)
    return instances


@pytest.mark.integration
async def test_handle_balance_scan_scoped_to_one_wallet(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_rpc_readers: list[_FakeRpcReader],
    test_secret_key: str,
) -> None:
    async with db_session_factory() as session:
        wallet_a = await add_wallet(session, address=_WALLET_A, label="A")
        await add_wallet(session, address=_WALLET_B, label="B")
        run_id = await enqueue_job(
            session,
            kind=JobKind.BALANCE_SCAN,
            params={"wallet_id": str(wallet_a.id)},
        )
        await session.commit()

    async with db_session_factory() as session:
        await worker_main.handle_balance_scan(session, run_id)

    assert len(fake_rpc_readers) == 1, "exactly one RpcReader session for the scoped scan"
    queried_addresses = {call[1] for call in fake_rpc_readers[0].calls}
    assert queried_addresses == {_WALLET_A}
    assert _WALLET_B not in queried_addresses, (
        "a wallet-scoped balance_scan must never query another wallet's address"
    )

    async with db_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT w.address FROM balance_observation bo"
                        " JOIN wallet w ON w.id = bo.wallet_id"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(rows) == {_WALLET_A}


@pytest.mark.integration
async def test_handle_balance_scan_scoped_wallet_inactive_completes_without_rpc(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_rpc_readers: list[_FakeRpcReader],
) -> None:
    """A stopped (or deleted) scoped wallet must complete cleanly, no fallback scan."""
    async with db_session_factory() as session:
        wallet_a = await add_wallet(session, address=_WALLET_A, label="A")
        await add_wallet(session, address=_WALLET_B, label="B")
        await stop_wallet(session, wallet_id=wallet_a.id)
        run_id = await enqueue_job(
            session,
            kind=JobKind.BALANCE_SCAN,
            params={"wallet_id": str(wallet_a.id)},
        )
        await session.commit()

    async with db_session_factory() as session:
        await worker_main.handle_balance_scan(session, run_id)

    assert fake_rpc_readers == [], (
        "an inactive scoped wallet must not trigger any RPC reads at all — "
        "falling back to scanning every active wallet is the RPC blow-up "
        "this feature exists to avoid"
    )


@pytest.mark.integration
async def test_handle_balance_scan_unscoped_still_scans_all_active_wallets(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_rpc_readers: list[_FakeRpcReader],
    test_secret_key: str,
) -> None:
    """A run with no params keeps today's "all active wallets" behaviour."""
    async with db_session_factory() as session:
        await add_wallet(session, address=_WALLET_A, label="A")
        await add_wallet(session, address=_WALLET_B, label="B")
        run_id = await enqueue_job(session, kind=JobKind.BALANCE_SCAN)
        await session.commit()

    async with db_session_factory() as session:
        await worker_main.handle_balance_scan(session, run_id)

    assert len(fake_rpc_readers) == 1
    queried_addresses = {call[1] for call in fake_rpc_readers[0].calls}
    assert queried_addresses == {_WALLET_A, _WALLET_B}


@pytest.mark.integration
async def test_handle_discovery_scoped_to_one_wallet(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []

    class _EmptyDiscoveryResult:
        candidates: list[Any] = []
        checkpoint: dict[str, Any] | None = None

    async def _fake_discover_tokens(
        _session: AsyncSession, *, wallet_address: str, **_kwargs: Any
    ) -> _EmptyDiscoveryResult:
        discovered.append(wallet_address)
        return _EmptyDiscoveryResult()

    monkeypatch.setattr(worker_main, "discover_tokens", _fake_discover_tokens)

    async with db_session_factory() as session:
        wallet_a = await add_wallet(session, address=_WALLET_A, label="A")
        await add_wallet(session, address=_WALLET_B, label="B")
        run_id = await enqueue_job(
            session,
            kind=JobKind.DISCOVERY,
            params={"wallet_id": str(wallet_a.id)},
        )
        await session.commit()

    async with db_session_factory() as session:
        await worker_main.handle_discovery(session, run_id)

    assert discovered == [_WALLET_A], (
        "a wallet-scoped discovery run must never discover for another wallet"
    )


@pytest.mark.integration
async def test_balance_scan_enqueues_a_valuation_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_rpc_readers: list[_FakeRpcReader],
    test_secret_key: str,
) -> None:
    """AUD-446: a finished scan must republish the portfolio.

    The dashboard renders the latest valuation_snapshot, not
    balance_observation. Without this enqueue, "Refresh balances" updated the
    observations and changed nothing the user could see until the next hourly
    quote_refresh happened to enqueue a valuation of its own.
    """
    async with db_session_factory() as session:
        await add_wallet(session, address=_WALLET_A, label="A")
        run_id = await enqueue_job(session, kind=JobKind.BALANCE_SCAN)
        await session.commit()

    async with db_session_factory() as session:
        await worker_main.handle_balance_scan(session, run_id)

    async with db_session_factory() as session:
        pending = (
            await session.execute(
                text("SELECT count(*) FROM job_run WHERE kind = 'valuation' AND status = 'pending'")
            )
        ).scalar_one()

    assert pending == 1, "balance_scan must queue exactly one valuation republish"
