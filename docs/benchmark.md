# Benchmark

Two reproducible reports, produced by `scripts/benchmark.py`:

1. **Warm read latency** — p50/p95/p99 for the warm dashboard and history reads
   against the reference scale fixture. This is the measurement **SC-004**
   ("p95 ≤ 3s for warm dashboard/history reads") is stated against.
2. **Catalog logical-call report** — how many logical provider calls one full
   catalog-driven balance scan costs, broken down by JSON-RPC method.

They are deliberately separate: the first is host- and disk-sensitive and will
differ on every machine; the second is an exact integer that depends only on
the catalog snapshot and the wallet count, and is identical everywhere.

## Running it

```bash
docker compose -f compose.test.yaml --profile benchmark run --rm benchmark
```

That is the whole command. `migrate-test` applies all Alembic migrations to the
ephemeral `db-test` database first; the benchmark then truncates every
application table, seeds the reference fixture, and prints both reports plus a
JSON blob of the full result. The run **exits non-zero when a measured p95
breaks the 3s SC-004 budget**, so it is a gate and not only a report.

Against an already-migrated database of your own:

```bash
DATABASE_URL=postgresql+psycopg://audr:audr@localhost:5433/audr_test \
  python scripts/benchmark.py
```

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--trials N` | `100` | Timed requests per target |
| `--warmup N` | `10` | Discarded warm-up requests per target |
| `--hours N` | `8760` | Hourly history points to seed. Lower values are for smoke runs only — **never** publish numbers from a reduced-scale run |
| `--only latency\|calls` | `all` | Run just one of the two reports |
| `--skip-seed` | off | Measure the database as it stands instead of truncating and re-seeding |
| `--p95-budget-ms N` | `3000` | The SC-004 budget to gate on |
| `--no-gate` | off | Report numbers without failing on a budget violation |
| `--json PATH` | — | Also write the full machine-readable report |
| `--force` | off | Permit truncating a database whose name does not contain `test` |

The script refuses to run against a database whose name does not contain
`test`, because seeding truncates every table.

## Methodology, and what the numbers do *not* include

- **Warm, not cold.** Each target gets `--warmup` discarded requests before the
  timed ones, so the connection pool, query plans and page cache are hot. SC-004
  is a warm-read target; cold-start latency is a different measurement.
- **In-process ASGI.** Requests go through `httpx.ASGITransport` directly
  against the FastAPI app, so the numbers exclude uvicorn, the reverse proxy and
  the network hop. They measure the application and database, which is where the
  risk is, but they are a **floor** for what a browser sees, not the whole
  story.
- **Percentiles are nearest-rank**, with no interpolation, so every reported
  value is a sample that was actually observed.
- **Non-empty results are enforced.** Each target must return at least one row
  or the run aborts. The first draft of this benchmark reported flattering
  history numbers because the fixture was dated 2025-01-01 while
  `GET /api/v1/history` selects relative to wall-clock time: every window but
  `all` was timing an empty result set. The fixture is now anchored so its
  newest point sits just before "now", and the guard makes that class of
  silent-zero regression a failure.
- **The logical-call report makes no network calls.** `RpcReader._call_raw` is
  replaced with a counter returning canned results, so the count is exact and
  reproducible. `_call_raw` is the boundary rather than the narrower `_call`
  because `get_block_time` (`eth_getBlockByNumber`) and `get_logs`
  (`eth_getLogs`) bypass `_call` — stubbing `_call` alone let those escape the
  counter *and* attempt real HTTP, which the scan's `except Exception` then
  swallowed, so the report silently undercounted. `_call_endpoint` is
  additionally patched to abort the run if anything ever reaches the transport.
  Balance persistence is stubbed out in that phase too: writing ~20k
  `balance_observation` rows would not make the call count any more accurate,
  only slower.
- **Measured against a clean tree.** The compose service bind-mounts
  `./backend`, so a run started from a working tree with unrelated uncommitted
  changes measures *those* changes. Reference numbers are produced from a
  dedicated `git worktree` containing only `main` plus the benchmark itself.

## Reference fixture

`backend/tests/fixtures/history_scale.py`, at its default scale:

| | |
|---|---|
| Wallets | 50 (all `active`) |
| Assets | 100 (all 18 decimals) |
| Held (wallet, asset) pairs | 500 |
| Hourly snapshots / history points | 8,760 (one year) |
| `valuation_line` rows | 4,380,000 (500 × 8,760) |
| Catalog | vendored Uniswap mainnet token list, 407 entries |

Every pair holds 1,000,000 whole units priced at $1.50, so each snapshot totals
$750,000,000 and spot-checks stay easy to verify by hand.

---

## Reference results

Produced **2026-10-02** by a single full-scale run:

```bash
docker compose -f compose.test.yaml --profile benchmark run --rm benchmark
```

### Reference host

| | |
|---|---|
| Platform | `Linux-5.15.0-191-generic-x86_64-with-glibc2.41` |
| CPU / RAM | 2 cores x86_64 / 11.7 GiB |
| Postgres | 16.15 (ephemeral `db-test`, container-local volume) |
| Python | 3.14.7, in container |
| 1m/5m/15m load at start | **1.72 / 0.69 / 0.43** — 0.86 per CPU |
| Benchmark peak RSS | 178 MiB |
| Fixture seed time | 906 s (~15 min), of which 896 s is the 4.38M-row `valuation_line` insert |

> **These are an upper bound, not a quiet-host figure.** 0.86 load per CPU is
> above the 0.7 contention threshold, so the run self-flagged
> `host.contended = true` and the latency numbers include scheduler delay from
> other work on the box. They are published anyway because the margin settles
> the question either way: the worst p95 is **20× inside** the SC-004 budget, so
> no plausible amount of contention relief changes the verdict. A quiet-host
> re-run would move these numbers down, never up. The catalog logical-call
> report below is unaffected — it is an exact count, not a timing.
>
> That direction was checked rather than assumed: two confirmation runs against
> the same seeded database (`--skip-seed --trials 20`) on a less loaded host
> returned a dashboard p95 of **106–115 ms** versus the 149 ms published here,
> and every history window came in at or below its table value. The published
> figures are the conservative ones.

### Warm read latency — SC-004

10 discarded warm-ups then 100 timed trials per target, nearest-rank
percentiles, in-process ASGI.

| Target | p50 | p95 | p99 | max | Rows | Response |
|---|---|---|---|---|---|---|
| `GET /api/v1/portfolio` (dashboard) | 98.92 ms | **149.42 ms** | 163.94 ms | 195.50 ms | 500 | 297 KiB |
| `GET /api/v1/history?period=24h` | 12.22 ms | **16.43 ms** | 18.18 ms | 21.94 ms | 24 | 6.4 KiB |
| `GET /api/v1/history?period=30d` | 33.20 ms | **52.62 ms** | 63.80 ms | 98.29 ms | 720 | 190 KiB |
| `GET /api/v1/history?period=1y` | 45.18 ms | **67.31 ms** | 77.22 ms | 91.61 ms | 501 | 132 KiB |
| `GET /api/v1/history?period=all` | 64.80 ms | **130.76 ms** | 165.32 ms | 185.35 ms | 2,000 | 527 KiB |

**SC-004: met.** The worst measured p95 is the dashboard at 149.42 ms against a
3,000 ms budget — **20.1× of headroom**. The gate exited zero.

Shape worth noting: the dashboard is the slowest target despite returning fewer
rows than `history_all`, because it aggregates the newest snapshot across all
500 held pairs, whereas history reads pre-aggregated `history_point` rows. The
history windows scale with returned points, not with the 4.38M `valuation_line`
rows behind them — `period=all` downsamples to 2,000 points, which is why it
stays under the dashboard.

### Catalog logical-call report

Exact, network-free count for one full catalog-driven balance scan. Identical on
every host.

| Method | Logical calls |
|---|---|
| `eth_call` | 20,350 |
| `eth_getBalance` | 50 |
| `eth_blockNumber` | 1 |
| `eth_chainId` | 1 |
| **Total** | **20,402** |

| | |
|---|---|
| Catalog snapshot | `vendored:3deceee47e622008`, 407 entries |
| Active wallets | 50 |
| Monitored pairs after discovery | 20,350 (407 × 50) |
| Logical calls per wallet | 408 |
| Transport requests, as implemented | 20,402 (one per logical call) |
| Transport requests if batched at 50 | 409 |
| Discovery persist time | 68.13 s |
| Scan floor at configured 10 calls/s | **2,040 s (34 min)** |
| Scan floor at `research.md` ceiling of 20 calls/s | 1,020 s (17 min) |

The 34-minute floor is the headline number here, and it is a scan-design
consequence, not a performance defect — see *Interpreting the logical-call
report* below.

### Fixture row counts as measured

| Table | Rows |
|---|---|
| `valuation_line` | 4,380,000 |
| `monitored_pair` | 20,350 |
| `valuation_snapshot` | 8,760 |
| `history_point` | 8,760 |
| `asset` | 507 |
| `catalog_entry` | 407 |
| `wallet` | 50 |

The report's own `host.git_commit` reads `unknown` because the benchmark
container has no `.git`; the measured tree was `main` at `8fb74b6` plus this
change's `scripts/benchmark.py` host-contention reporting, which touches only
what the report *records*, never what it measures.

---

## Interpreting the logical-call report

The report is the honest cost of catalog-driven discovery, and it is large:
discovery turns the 407-entry catalog into a monitored pair for **every** active
wallet, and `balance_scan` then reads each pair individually.

Two observations worth acting on, neither of which is a bug in this benchmark:

- **`RpcReader` has no JSON-RPC batch path.** `research.md` specifies transport
  batches of 50 calls; the implementation issues one HTTP request per logical
  call. The report prints both `transport_requests_actual` and
  `transport_requests_if_batched_50` so the gap is visible. Batching would not
  reduce the logical (billed) call count — providers meter per method — but it
  would cut round trips by ~50×.
- **The scan is rate-limit bound, not latency bound.** At the default
  `rpc_rate_limit_per_second` of 10, a full scan cannot finish faster than
  `logical_calls / 10` seconds no matter how fast the provider answers. The
  report prints that floor for both the configured rate and the 20 calls/second
  ceiling from `research.md`. This is a property of fanning the whole catalog
  across every wallet; narrowing discovery (e.g. seeding monitored pairs from
  observed `Transfer` logs instead of the full catalog) is the lever that moves
  it, not a bigger rate limit.

Neither affects SC-004, which is about warm *read* latency — the dashboard and
history endpoints read the published snapshot out of Postgres and never touch a
provider.

## Related

- `docs/verification.md` — how to run every suite, and the recorded results.
- `docs/verification-history.md` — the scale fixture's own generation timings
  and post-generation SQL spot-checks.
- `specs/001-ethereum-portfolio/plan.md` — where SC-004 is stated.
- `specs/001-ethereum-portfolio/research.md` — where the batch size and the
  20 logical calls/second ceiling are specified.
