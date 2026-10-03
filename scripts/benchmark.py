#!/usr/bin/env python3
"""Warm dashboard/history latency benchmark + catalog logical-call report (T095 / AUD-108).

Two independent reports, deliberately kept separate because they measure
different things and have different failure modes:

1. **Warm read latency** (`--only latency`) — p50/p95/p99 for the warm
   dashboard (`GET /api/v1/portfolio`) and history (`GET /api/v1/history`)
   reads against the reference scale fixture, plus peak RSS and the fixture's
   row counts. This is the measurement SC-004 ("p95 <= 3s for warm
   dashboard/history reads") is stated against, so by default the script exits
   non-zero when a measured p95 breaks the budget.

2. **Catalog logical-call report** (`--only calls`) — how many *logical*
   provider calls one full catalog-driven balance scan costs for the reference
   fixture, broken down by JSON-RPC method. "Logical" is the billing unit from
   research.md: a provider counts each method invocation, even when several
   share one transport request. No network traffic happens — RpcReader._call_raw
   is replaced with a counter, so the number is exact and the report is
   reproducible on any host.

Usage (one command, from the repo root):

    docker compose -f compose.test.yaml --profile benchmark run --rm benchmark

Or directly against any migrated test database:

    DATABASE_URL=postgresql+psycopg://audr:audr@localhost:5433/audr_test \
      python scripts/benchmark.py

The script TRUNCATEs every application table before seeding, so it refuses to
run against a database whose name does not contain "test" unless --force is
passed. Publish numbers only from a full-scale run (--hours 8760, the default).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
# tests.fixtures.history_scale is the reference fixture generator; audr lives
# under backend/src. Both are importable from a plain checkout without an
# editable install, which is what makes this runnable in Dockerfile.test and
# on a developer host alike.
for _path in (str(BACKEND), str(BACKEND / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

_DEFAULT_DB_URL = "postgresql+psycopg://audr:audr@localhost:5433/audr_test"
# Benchmark-only key: this process creates a throwaway owner account in the
# ephemeral test database to obtain a session cookie. Never a real secret.
_BENCH_SECRET_KEY = "b" * 64
_BENCH_PASSWORD = "Benchmark1P@ssword"  # noqa: S105 — throwaway owner in the ephemeral test DB

_ETH_MAINNET_CHAIN_ID = 1
_STUB_BLOCK_NUMBER = 18_000_000
# Canned RPC results for the counted scan, keyed by JSON-RPC method.
#
# These are patched in at `RpcReader._call_raw`, which is the single funnel every
# method goes through on its way to the transport. Stubbing the narrower `_call`
# instead would miss `get_block_time` (eth_getBlockByNumber) and `get_logs`
# (eth_getLogs), which call `_call_raw` directly — those would escape the counter
# *and* attempt real outbound HTTP.
#
# Shapes must satisfy each caller's parsing (`_parse_hex_int`, the
# eth_getBlockByNumber `timestamp` lookup); otherwise the scan logs an exception,
# swallows it, and the per-method counts come out short.
_STUB_RESULTS: dict[str, Any] = {
    "eth_chainId": hex(_ETH_MAINNET_CHAIN_ID),
    "eth_blockNumber": hex(_STUB_BLOCK_NUMBER),
    "eth_getBalance": hex(10**18),
    "eth_call": "0x" + f"{10**18:064x}",
    # 2023-09-29T00:00:00Z — a plausible mainnet timestamp for block 18,000,000.
    "eth_getBlockByNumber": {
        "number": hex(_STUB_BLOCK_NUMBER),
        "timestamp": hex(1_695_945_600),
    },
    "eth_getLogs": [],
}
# research.md: transport batches of 50 calls, shared 20 logical calls/second
# ceiling. Reported as the derived floor for a full scan.
_TRANSPORT_BATCH_SIZE = 50
_SPEC_RATE_CEILING_PER_S = 20.0

# 1-minute load average per CPU above which measured latency is dominated by
# host contention rather than by the application. 1.0 would be the textbook
# line; 0.7 leaves headroom because the benchmark itself contributes load.
CONTENTION_THRESHOLD = 0.7

_LATENCY_TARGETS: list[tuple[str, str, dict[str, str]]] = [
    ("dashboard", "/api/v1/portfolio", {}),
    ("history_24h", "/api/v1/history", {"period": "24h"}),
    ("history_30d", "/api/v1/history", {"period": "30d"}),
    ("history_1y", "/api/v1/history", {"period": "1y"}),
    ("history_all", "/api/v1/history", {"period": "all"}),
]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile — no interpolation, so every reported value is
    an actually observed sample."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    rank = max(1, -(-int(pct * len(ordered) * 100) // 10000))  # ceil(pct/100 * n)
    return ordered[min(rank, len(ordered)) - 1]


def _result_size(body: object) -> int:
    """Rows the response actually carries: history entries, or dashboard holdings."""
    if not isinstance(body, dict):
        return 0
    for key in ("entries", "holdings"):
        value = body.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _summarise(samples_ms: list[float]) -> dict[str, float]:
    return {
        "n": len(samples_ms),
        "min_ms": round(min(samples_ms), 2),
        "p50_ms": round(_percentile(samples_ms, 50), 2),
        "p95_ms": round(_percentile(samples_ms, 95), 2),
        "p99_ms": round(_percentile(samples_ms, 99), 2),
        "max_ms": round(max(samples_ms), 2),
        "mean_ms": round(sum(samples_ms) / len(samples_ms), 2),
    }


# ---------------------------------------------------------------------------
# Host / fixture identity
# ---------------------------------------------------------------------------


def _git_commit() -> str:
    try:
        # S603/S607: fixed argv, shell=False; resolving `git` from PATH is intended —
        # this only stamps the report with the commit under test.
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _mem_total_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _loadavg() -> list[float] | None:
    try:
        parts = Path("/proc/loadavg").read_text().split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except (OSError, ValueError, IndexError):
        return None


def _host_spec() -> dict[str, Any]:
    spec: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "mem_total_gib": _mem_total_gib(),
        "python": platform.python_version(),
        "in_container": Path("/.dockerenv").exists(),
        "git_commit": _git_commit(),
    }
    # A latency number from a contended host measures the contention, not the
    # application, so the report must not be readable without the load that
    # produced it. The first two attempts at a reference run were made at
    # load ~11 on 2 cores and nothing in the output said so.
    load = _loadavg()
    cpus = os.cpu_count() or 1
    if load is not None:
        spec["loadavg_1_5_15"] = load
        spec["load_per_cpu_at_start"] = round(load[0] / cpus, 2)
        spec["contended"] = load[0] / cpus > CONTENTION_THRESHOLD
    return spec


def _peak_rss_mib() -> float:
    # ru_maxrss is KiB on Linux.
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


# ---------------------------------------------------------------------------
# Database preparation
# ---------------------------------------------------------------------------

_COUNT_TABLES = (
    "wallet",
    "asset",
    "monitored_pair",
    "valuation_snapshot",
    "valuation_line",
    "history_point",
    "catalog_entry",
)


async def _row_counts(session: AsyncSession) -> dict[str, int]:
    import sqlalchemy as sa

    counts: dict[str, int] = {}
    for table in _COUNT_TABLES:
        # table comes from the hardcoded _COUNT_TABLES tuple above, not user input.
        result = await session.execute(sa.text(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
        counts[table] = int(result.scalar_one())
    return counts


async def _assert_migrated(session: AsyncSession) -> None:
    import sqlalchemy as sa

    result = await session.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = ANY(:names)"
        ),
        {"names": list(_COUNT_TABLES)},
    )
    if int(result.scalar_one()) != len(_COUNT_TABLES):
        raise SystemExit(
            "Database schema is incomplete — run `alembic upgrade head` against it first"
        )


async def _truncate_all(session: AsyncSession) -> None:
    """Empty every application table (alembic_version excluded) so the fixture
    identity in the report is exactly what this run inserted."""
    import sqlalchemy as sa

    result = await session.execute(
        sa.text(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            "   AND table_name <> 'alembic_version'"
        )
    )
    tables = [row[0] for row in result]
    if not tables:
        return
    quoted = ", ".join(f'"{t}"' for t in tables)
    # tables/quoted come from information_schema.tables, not user input, and are identifier-quoted.
    await session.execute(sa.text(f"TRUNCATE {quoted} CASCADE"))  # noqa: S608


def _guard_destructive(db_url: str, *, force: bool) -> None:
    db_name = db_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" in db_name.lower() or force:
        return
    raise SystemExit(
        f"Refusing to truncate database {db_name!r}: the benchmark wipes every table.\n"
        "Point it at a dedicated test database, or pass --force if you really mean this one."
    )


# ---------------------------------------------------------------------------
# Phase 1 — warm read latency
# ---------------------------------------------------------------------------


async def _measure_latency(
    factory: async_sessionmaker[AsyncSession],
    *,
    trials: int,
    warmup: int,
) -> dict[str, Any]:
    import httpx
    import sqlalchemy as sa
    from audr.api.app import app
    from audr.db import get_db

    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    # POST /setup returns 409 once an owner exists, which is what --skip-seed
    # leaves behind on a second run. The auth rows are not part of the fixture
    # under measurement, so clear them unconditionally.
    async with factory() as session:
        async with session.begin():
            # table is one of the three hardcoded literals in this tuple, not user input.
            for table in ("session", "login_attempt", "owner"):
                await session.execute(sa.text(f"DELETE FROM {table}"))  # noqa: S608

    app.dependency_overrides[get_db] = _override
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://benchmark") as client:
            setup = await client.post("/api/v1/setup", json={"password": _BENCH_PASSWORD})
            if setup.status_code != 201:
                raise SystemExit(
                    f"Benchmark owner setup failed ({setup.status_code}): {setup.text}"
                )

            results: dict[str, Any] = {}
            for name, path, params in _LATENCY_TARGETS:
                # Warm the connection pool, the query plans and the page cache.
                # Cold-start numbers are not what SC-004 is stated against.
                # max(1, …): one probe always runs, because the empty-result
                # guard below needs a body to inspect even at --warmup 0.
                for _ in range(max(1, warmup)):
                    probe = await client.get(path, params=params)
                    if probe.status_code != 200:
                        raise SystemExit(
                            f"{name} ({path}) returned {probe.status_code}: {probe.text}"
                        )

                # A 200 with an empty body is the failure mode that made the
                # first run of this benchmark meaningless: history windows are
                # relative to now, so a fixture dated in the past timed an
                # empty result set and reported flattering numbers. Refuse to
                # publish a target that touched no rows.
                rows = _result_size(probe.json())
                if rows == 0:
                    raise SystemExit(
                        f"{name} ({path}) returned an empty result set — the fixture does not"
                        " cover this window, so its latency would be meaningless"
                    )

                samples_ms: list[float] = []
                payload_bytes = 0
                for _ in range(trials):
                    started = time.perf_counter()
                    response = await client.get(path, params=params)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    if response.status_code != 200:
                        raise SystemExit(
                            f"{name} ({path}) returned {response.status_code}: {response.text}"
                        )
                    samples_ms.append(elapsed_ms)
                    payload_bytes = len(response.content)

                summary = _summarise(samples_ms)
                summary["response_bytes"] = payload_bytes
                summary["result_rows"] = rows
                results[name] = summary
                print(
                    f"  {name:<14} p50={summary['p50_ms']:>8.2f}ms"
                    f"  p95={summary['p95_ms']:>8.2f}ms"
                    f"  p99={summary['p99_ms']:>8.2f}ms"
                    f"  max={summary['max_ms']:>8.2f}ms"
                    f"  ({rows} rows, {payload_bytes} B)"
                )
            return results
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Phase 2 — catalog logical-call report
# ---------------------------------------------------------------------------


async def _measure_catalog_calls(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    import sqlalchemy as sa
    from audr.assets.catalog import import_catalog
    from audr.config import get_settings
    from audr.jobs import __main__ as jobs_main
    from audr.portfolio.discovery import discover_tokens, persist_discovery_candidates
    from audr.providers.rpc_reader import RpcReader
    from audr.wallets.service import list_wallets

    # 1. Import the vendored catalog, then run real discovery for every active
    #    wallet so monitored_pair reflects catalog coverage rather than a
    #    hand-made subset.
    async with factory() as session:
        async with session.begin():
            version = await import_catalog(session)
            catalog_commit = str(version.commit_hash)
            # Counted in SQL rather than via version.entries: the relationship
            # is lazy, and import_catalog returns the pre-existing row
            # untouched when the snapshot is already imported.
            catalog_entries = int(
                (
                    await session.execute(
                        sa.text("SELECT COUNT(*) FROM catalog_entry WHERE version_id = :vid"),
                        {"vid": str(version.id)},
                    )
                ).scalar_one()
            )

    async with factory() as session:
        wallets = [w for w in await list_wallets(session) if w.status == "active"]

    started = time.perf_counter()
    candidate_total = 0
    for wallet in wallets:
        async with factory() as session:
            async with session.begin():
                result = await discover_tokens(
                    session,
                    wallet_address=wallet.address,
                    use_catalog=True,
                    manual_addresses=[],
                    checkpoint=None,
                )
                candidate_total += len(result.candidates)
                await persist_discovery_candidates(
                    session,
                    wallet_address=wallet.address,
                    candidates=result.candidates,
                )
    discovery_s = round(time.perf_counter() - started, 2)
    print(
        f"  discovery: {len(wallets)} wallets x {candidate_total // max(1, len(wallets))}"
        f" candidates persisted in {discovery_s}s"
    )

    # 2. Run the real balance_scan handler with the provider boundary replaced
    #    by a counter. Persistence is stubbed too: this report is about
    #    provider call volume, and writing ~20k balance_observation rows would
    #    make the count no more accurate, only slower.
    counts: Counter[str] = Counter()
    original_call_raw = RpcReader._call_raw
    original_record = jobs_main.record_balance

    # ANN401: `Any` mirrors the real RpcReader._call_raw signature — results are
    # per-method JSON (str, dict or list) and callers narrow them themselves.
    async def _counting_call_raw(self: object, method: str, params: list) -> Any:  # noqa: ARG001, ANN401
        counts[method] += 1
        try:
            return _STUB_RESULTS[method]
        except KeyError as exc:  # pragma: no cover - guards a silent undercount
            # Hard failure rather than a fallback value: callers in the scan path
            # wrap provider errors in `except Exception` and carry on, so a
            # missing stub would otherwise be swallowed and silently undercount.
            raise SystemExit(
                f"benchmark stub has no canned result for RPC method {method!r}"
            ) from exc

    async def _noop_record(*_args: object, **_kwargs: object) -> None:
        return None

    # Belt-and-braces: if a future code path reaches the transport without going
    # through _call_raw, fail loudly instead of quietly making real HTTP requests
    # (and reporting an undercount). The benchmark container has no provider-mock
    # and no egress, so such a call would otherwise just time out and be
    # swallowed by the scan's `except Exception`.
    original_endpoint = RpcReader._call_endpoint

    async def _forbidden_endpoint(*_args: object, **_kwargs: object) -> NoReturn:
        raise SystemExit(
            "benchmark reached RpcReader._call_endpoint — a provider call escaped"
            " the counter, so the logical-call report would be an undercount"
        )

    RpcReader._call_raw = _counting_call_raw  # type: ignore[method-assign]
    RpcReader._call_endpoint = _forbidden_endpoint  # type: ignore[method-assign]
    jobs_main.record_balance = _noop_record  # type: ignore[assignment]
    try:
        async with factory() as session:
            await jobs_main.handle_balance_scan(session, run_id=uuid.uuid4())
    finally:
        RpcReader._call_raw = original_call_raw  # type: ignore[method-assign]
        RpcReader._call_endpoint = original_endpoint  # type: ignore[method-assign]
        jobs_main.record_balance = original_record  # type: ignore[assignment]

    total = sum(counts.values())
    settings = get_settings()
    report = {
        "catalog_commit": catalog_commit,
        "catalog_entries": catalog_entries,
        "active_wallets": len(wallets),
        "discovery_candidates_total": candidate_total,
        "discovery_persist_s": discovery_s,
        "monitored_pairs": candidate_total,
        "logical_calls_by_method": dict(sorted(counts.items())),
        "logical_calls_total": total,
        "logical_calls_per_wallet": (round(total / len(wallets), 1) if wallets else 0),
        # Transport requests the current implementation actually issues: one
        # per logical call, because RpcReader has no JSON-RPC batch path.
        "transport_requests_actual": total,
        "transport_requests_if_batched_50": -(-total // _TRANSPORT_BATCH_SIZE),
        "configured_rate_limit_per_s": settings.rpc_rate_limit_per_second,
        "floor_seconds_at_configured_rate": round(total / settings.rpc_rate_limit_per_second, 1),
        "floor_seconds_at_spec_ceiling_20_per_s": round(total / _SPEC_RATE_CEILING_PER_S, 1),
    }
    for method, count in report["logical_calls_by_method"].items():
        print(f"  {method:<18} {count:>8}")
    print(f"  {'TOTAL':<18} {total:>8} logical calls")
    print(
        f"  floor at configured {settings.rpc_rate_limit_per_second}/s:"
        f" {report['floor_seconds_at_configured_rate']}s"
        f"  (spec ceiling 20/s: {report['floor_seconds_at_spec_ceiling_20_per_s']}s)"
    )
    return report


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from tests.fixtures.history_scale import generate as generate_scale_fixture

    engine = create_async_engine(args.db_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    report: dict[str, Any] = {
        "host": _host_spec(),
        "parameters": {
            "trials": args.trials,
            "warmup": args.warmup,
            "hourly_points": args.hours,
            "seeded": not args.skip_seed,
            "only": args.only,
            "p95_budget_ms": args.p95_budget_ms,
        },
    }
    try:
        async with factory() as session:
            await _assert_migrated(session)
            pg_version = str((await session.execute(sa.text("SHOW server_version"))).scalar_one())
        report["host"]["postgres_version"] = pg_version

        if not args.skip_seed:
            print(f"==> Seeding reference fixture ({args.hours} hourly points)")
            async with factory() as session:
                async with session.begin():
                    await _truncate_all(session)
            # Anchor the newest point just before now: GET /api/v1/history
            # windows are relative to wall-clock time, so the fixture's default
            # 2025-01-01 epoch would leave 24h/7d/30d/90d/1y measuring an empty
            # result set and only `all` touching real rows.
            base_time = datetime.now(tz=UTC) - timedelta(hours=args.hours - 1)
            async with factory() as session:
                async with session.begin():
                    seed_stats = await generate_scale_fixture(
                        session, hourly_points=args.hours, base_time=base_time
                    )
            report["fixture_seed_stats"] = seed_stats
            for key, value in seed_stats.items():
                print(f"  {key}: {value}")
        else:
            print("==> Skipping seed (--skip-seed): using the database as it stands")

        if args.only in ("all", "latency"):
            print(f"==> Warm read latency ({args.warmup} warmup + {args.trials} trials)")
            report["latency"] = await _measure_latency(
                factory, trials=args.trials, warmup=args.warmup
            )

        if args.only in ("all", "calls"):
            print("==> Catalog logical-call report")
            report["catalog_calls"] = await _measure_catalog_calls(factory)

        async with factory() as session:
            report["fixture_row_counts"] = await _row_counts(session)
        report["peak_rss_mib"] = _peak_rss_mib()
    finally:
        await engine.dispose()
    return report


def _gate(report: dict[str, Any], budget_ms: float) -> list[str]:
    """Return SC-004 violations: any measured p95 over the budget."""
    violations = []
    for name, summary in report.get("latency", {}).items():
        if summary["p95_ms"] > budget_ms:
            violations.append(f"{name} p95={summary['p95_ms']}ms > budget {budget_ms}ms")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Warm dashboard/history p50/p95 benchmark and catalog logical-call report",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or _DEFAULT_DB_URL,
        help="Async SQLAlchemy URL of a migrated test database",
    )
    parser.add_argument("--trials", type=int, default=100, help="Timed requests per target")
    parser.add_argument("--warmup", type=int, default=10, help="Discarded warm-up requests")
    parser.add_argument(
        "--hours",
        type=int,
        default=8_760,
        help="Hourly history points to seed (8760 = the reference fixture; lower only for"
        " smoke runs, never for published numbers)",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Measure the database as it stands instead of truncating and re-seeding",
    )
    parser.add_argument(
        "--only",
        choices=("all", "latency", "calls"),
        default="all",
        help="Run only one of the two reports",
    )
    parser.add_argument(
        "--p95-budget-ms",
        type=float,
        default=3_000.0,
        help="SC-004 budget; a measured p95 above this fails the run",
    )
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="Report numbers without failing the run on an SC-004 violation",
    )
    parser.add_argument("--json", dest="json_out", help="Also write the full report to this path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow truncating a database whose name does not contain 'test'",
    )
    args = parser.parse_args()

    if not args.skip_seed:
        _guard_destructive(args.db_url, force=args.force)

    # audr.config caches Settings on first import, so the environment must be
    # correct before anything under audr is imported.
    os.environ["DATABASE_URL"] = args.db_url
    os.environ.setdefault("SECRET_KEY", _BENCH_SECRET_KEY)

    print(f"==> audr benchmark — db={args.db_url}")
    start_spec = _host_spec()
    if start_spec.get("contended"):
        print(
            f"==> WARNING: host is contended — 1m load {start_spec['loadavg_1_5_15'][0]}"
            f" over {start_spec['cpu_count']} cpu"
            f" ({start_spec['load_per_cpu_at_start']} per cpu, threshold"
            f" {CONTENTION_THRESHOLD}).\n"
            "    Latency numbers from this run measure host contention as much as the\n"
            "    application. Treat them as an upper bound, do not publish them as\n"
            "    reference figures, and re-run on a quiet host. The catalog\n"
            "    logical-call report is unaffected: it is an exact count, not a timing."
        )
    report = asyncio.run(_run(args))

    print("==> Summary")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
        )
        print(f"==> Wrote {args.json_out}")

    if args.no_gate:
        print("==> Gate disabled (--no-gate): numbers reported, SC-004 not enforced")
        return 0
    if not report.get("latency"):
        # `--only calls` produces no latency samples. Saying "budget met" here
        # would claim an SC-004 pass that nothing in this run actually measured.
        print("==> SC-004 not evaluated: no latency measured (--only calls)")
        return 0

    violations = _gate(report, args.p95_budget_ms)
    if violations:
        print("==> SC-004 FAILED")
        for violation in violations:
            print(f"  {violation}")
        if report.get("host", {}).get("contended"):
            print(
                "    NOTE: this host was contended at start (see host.loadavg_1_5_15).\n"
                "    Confirm on a quiet host before treating this as a real regression."
            )
        return 1
    print(f"==> SC-004 budget met (all p95 <= {args.p95_budget_ms}ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
