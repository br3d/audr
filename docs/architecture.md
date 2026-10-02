# Architecture

How audr is actually built, as of migration head `0016`. This is the document to
read before changing anything structural. It describes the implementation, not
the specification — where the two diverge, the divergence is named here and
tracked in [release-1-coverage.md](release-1-coverage.md).

Companion documents: [api.md](api.md) for the route-by-route HTTP contract,
[containers.md](containers.md) for the deployment topology rationale,
[development.md](development.md) for how to run and test all of this.

---

## 1. Processes

audr is two application processes and a database, not one monolith.

| Process | Entrypoint | Role |
|---|---|---|
| `api` | `uvicorn audr.api.app:app` | Serves `/api/v1/*`, `/health/*` **and the compiled SPA**. No background work, no startup hooks. The only service with a published port (`80:8000`). |
| `worker` | `python -m audr.jobs` | Every periodic and on-demand job. Polls the `job_run` queue every 5 s. |
| `migrate` | `alembic upgrade head && python -m audr.operations.init_key` | One-shot bootstrap. Exits 0 and stays exited. |
| `db` | PostgreSQL 16 | All persistent state, in the `db_data` named volume. |

There used to be a fifth service, `web` (nginx), which served the SPA and
reverse-proxied `^/(api|health)/` to `api:8000`. AUD-388 folded it into `api`
— see [containers.md](containers.md) for the rationale and the trade-off.

The api/worker split is deliberate and load-bearing. `create_app()` installs
middleware, exception handlers and routers — nothing else. A long RPC scan or a
wedged provider call therefore cannot starve HTTP request handling, the worker
can be restarted without dropping the API, and `docker logs audr-worker-1` is a
clean record of job behaviour on its own. The cost is one extra container; the
reasoning, and the comparison against rotki's single-container design, is in
[containers.md](containers.md).

### The request path

```
Browser ──► api:8000 (published on :80)
              ├── /api/v1/*, /health/*  ──► routers
              └── everything else       ──► index.html (SPA fallback)
                    except /api, /health  ──► JSON 404
```

Routers are registered first and the SPA is mounted last, so the catch-all only
sees what nothing else claimed. `/api` and `/health` are reserved against the
fallback so a mistyped endpoint returns the JSON error envelope rather than 200
`text/html` — see `backend/src/audr/api/spa.py`.

The SPA fallback is still a trap worth knowing: a bare `GET /` returns 200 from
`index.html` whenever the process is up, regardless of whether the *database*
is reachable. **`GET /health/ready` is the only trustworthy liveness signal**,
and it must be checked for a `"status":"ok"` body, not just a 200. Every deploy
gate and smoke test in this repository does exactly that.

---

## 2. Backend package map

Everything lives under `backend/src/audr/`.

### Top level

| Module | Purpose |
|---|---|
| `config.py` | pydantic-settings `Settings` + lru-cached `get_settings()`. Reads `.env`. Validates that `DATABASE_URL` uses the `postgresql+psycopg://` scheme. |
| `db.py` | Async engine and `async_sessionmaker` factories (lru-cached) plus the `get_db()` FastAPI dependency. |
| `models.py` | `DeclarativeBase` and the shared `MetaData` with `ix_/uq_/ck_/fk_/pk_` naming conventions. |

### `api/` — the HTTP layer

`app.py` builds the application: CORS (empty `allow_origins`, credentials on),
a request-id middleware that stamps `X-Request-Id`, a middleware that forces
`Cache-Control: no-store` on `/api/` responses unless the route set its own,
three exception handlers, and router registration. Swagger and Redoc are
**disabled** (`docs_url=None`) — the API surface is documented in
[api.md](api.md), not served.

`errors.py` owns the single error envelope every failure uses:

```json
{"error": {"code": "...", "message": "...", "field_errors": null, "retryable": false},
 "request_id": "..."}
```

Handlers cover `StarletteHTTPException`, `RequestValidationError` and bare
`Exception`. The 500 handler never echoes exception text — no stack traces, no
upstream provider bodies, no credentials reach a client.

The remaining modules are feature routers: `health`, `auth`, `wallets`,
`holdings`, `portfolio`, `assets`, `history`, `events`, `news`, `integrations`,
`settings`. The last one is larger than its name suggests — it also carries
jobs, status, exports and the provider-purge routes.

### Domain packages

| Package | Contents |
|---|---|
| `auth/` | `Owner`, `Session`, `LoginAttempt` ORM; Argon2id hashing, session lifecycle, failed-login throttling. |
| `wallets/` | `Wallet` ORM; add/label/stop/reactivate/list. |
| `assets/` | Asset identity and catalogs: `Asset`, `AssetMetadataRevision`, `CatalogVersion`/`CatalogEntry`, `CmcMapVersion`/`CmcMapEntry`, `MonitoredPair`, `DiscoveryCoverage`, `BalanceObservation`. Plus `catalog.py` (import the vendored Uniswap token list), `cmc_catalog.py` (address/symbol → CoinMarketCap id resolution), `constants.py` (the native-ETH sentinel `0xeeee…eeee`) and `data/` (vendored JSON snapshots). |
| `portfolio/` | The accounting core. `money.py` — Decimal-only raw→quantity→USD math. `balances.py` — exact uint256 recording and latest-observation reads. `discovery.py` — chunked, resumable ERC-20 candidate discovery with checkpoints. `snapshot.py` — `publish_valuation_snapshot()`, holdings × latest complete quote set → quality flags, deduplicated by a deterministic `input_key`. `history.py` / `history_query.py` — materialise history points, then period selection, gap markers and cursor pagination over them. `models.py` — `QuoteSet`, `QuoteObservation`, `ValuationSnapshot`, `ValuationLine`. |
| `providers/` | Every outbound integration (§5). |
| `settings/` | `integrations.py` — the encrypted, revision-checked integration blob store. `quotes.py` — CoinGecko key get/set on top of it. `schedules.py` — schedule CRUD with freshness/budget validation and usage projections. |
| `jobs/` | The worker (§4). |
| `operations/` | Cross-cutting operational concerns: `crypto.py` (AES-256-GCM envelope), `init_key.py` (master-key lifecycle), `migrations.py` (readiness vs alembic head), `status.py` (status dataclasses), `exports.py` (streamed JSON/CSV), `csv_safe.py` (formula-injection neutralisation), `purge.py` (password-confirmed provider purge), `cleanup.py` (prune expired sessions and login attempts), `reset_password.py` (host-side emergency reset). |

The dependency direction is one-way: `api/` depends on the domain packages,
domain packages depend on `providers/` and `operations/`, and nothing depends
back on `api/`. The worker imports the same domain packages the API does, which
is why they share one image.

---

## 3. Data model

PostgreSQL 16. All primary keys are UUIDs defaulted by `pgcrypto`'s
`gen_random_uuid()`; all timestamps are `timestamptz`. Monetary and quantity
columns are `numeric` — never float.

Migrations are a linear chain from the squashed baseline **`0001`** to head
**`0016`**. `0001_baseline.py` has `down_revision = None` and replaces the
original 001–008; see the AUD-306 history if you need the pre-squash tree.

### Identity and ownership

| Table | Notes |
|---|---|
| `owner` | Exactly one row, enforced by a partial unique index on `singleton WHERE singleton`. Holds the Argon2id hash. |
| `session` | Opaque server-side sessions: `csrf_token`, `revoked`, `expires_at`, `last_active_at`. |
| `login_attempt` | `attempted_at`, `success` — the throttle's input. Pruned by the worker. |
| `wallet` | `address` with a CHECK constraint forcing lowercase storage, `label`, `status ∈ active|stopped`. |
| `asset` | `token_address` (CHECK lowercase), `symbol`, `name`, `decimals`, `source ∈ catalog|manual`, `excluded`, `decimals_override`, `price_unavailable_since`. |
| `asset_metadata_revision` | Append-only metadata history, so a historical row keeps the decimals and symbol that applied when it was recorded. |

### Observation and valuation

The central invariant is that **observations are immutable and valuations are
derived**. Nothing rewrites a published row.

| Table | Notes |
|---|---|
| `balance_observation` | `raw_amount numeric(78,0)` — the exact uint256, never a float. Carries `block_number`, `block_time` and `observed_at` separately, because block freshness and read freshness are different facts. |
| `balance_observation_invalidation` | Reorg handling: an observation is marked invalid rather than deleted or silently corrected. |
| `monitored_pair` | The (wallet, asset) work list a balance scan iterates. |
| `discovery_coverage` | Per-wallet discovery progress — what makes coverage honest instead of implied. |
| `quote_set` / `quote_observation` | A quote set is a batch with a status (`pending|complete|failed|empty`); observations carry `price_usd numeric(36,18)` with a CHECK > 0. The `empty` status exists so an empty successful fetch cannot shadow previously known prices. |
| `valuation_snapshot` | `quality ∈ complete|partial|stale|gaps|unknown`, plus a NOT NULL UNIQUE `input_key` that deterministically dedupes a recomputation of identical inputs. |
| `valuation_line` | One row per contributing holding, with `observation_id` provenance so any total can be traced back to the exact reads behind it. |
| `history_point` | The chart series: `total_value_usd`, `quality`, `included_wallet_count`, `included_asset_count`, `has_gap`, `is_canonical`. |

### Operations

| Table | Notes |
|---|---|
| `integration` | `kind ∈ rpc|coingecko`, a monotonic `revision` for optimistic concurrency, and `encrypted_blob bytea`. |
| `key_state` | The wrapped master key. |
| `job_run` | `kind`, `status ∈ pending|in_progress|completed|failed|cancelled`, `worker_id`, `retry_count`, `max_retries` (3), `error`, `checkpoint`, `claimed_at`, `heartbeat_at`. A partial unique index `ON (kind) WHERE status='in_progress'` is what guarantees one active run per kind. |
| `schedule` | `enabled`, `revision`, `paused_at`, `freshness_s`, `budget_calls_per_day`, `last_run_at`, `next_run_at`. |
| `worker_status` | Heartbeat and current job, surfaced by `/health/ready` and `/api/v1/status`. |
| `operational_event`, `provider_budget`, `provider_purge_log` | Audit and budget bookkeeping. |

### Extensions beyond release 1

`onchain_event` and `event_indexer_checkpoint` (incremental `eth_getLogs`
Transfer/Approval indexing), `asset_news`, `cmc_map_version`/`cmc_map_entry`
(CoinMarketCap id resolution) and `asset_icon` (the icon cache, with
`status ∈ ok|missing` for negative caching). These back the events, allowances,
news and icon features, which the release-1 spec explicitly excludes — they are
implemented extensions, not spec requirements.

---

## 4. Background jobs

`python -m audr.jobs` bootstraps the vendored catalog and the CoinMarketCap map
(both idempotent and non-fatal on failure, so a bad snapshot cannot block
startup), registers SIGTERM/SIGINT handlers, takes its worker id from the
hostname, and enters a poll loop with a 5 s idle interval. Each cycle runs every
registered worker once, then `recheck_canonicality()` and
`cleanup_expired_auth_rows()`, then writes a liveness heartbeat.

| Job kind | Trigger | What it does |
|---|---|---|
| `balance_scan` | schedule (fallback 300 s) | Reads native and ERC-20 balances for every monitored pair. |
| `discovery` | schedule (fallback 86400 s) | Chunked, resumable ERC-20 candidate discovery against the catalog. |
| `quote_refresh` | schedule (fallback 3600 s) | Prices **held assets only**; enqueues a `valuation` run on success. |
| `event_indexer` | schedule (fallback 300 s) | Incremental `eth_getLogs` Transfer and Approval indexing per wallet, bounded by a per-run chunk budget. |
| `news_refresh` | schedule (fallback 900 s) | CoinGecko news matched to held assets. Returns early without a key. |
| `asset_icon_refresh` | schedule (fallback 900 s) | Fills the `asset_icon` cache for held assets, with negative caching. |
| `valuation` | on demand | Publishes a valuation snapshot and materialises a history point. |
| `validate_rpc`, `validate_quotes` | on demand | Probe exactly the configured endpoint and record a sanitized outcome. |

Reliability mechanics live in `jobs/store.py`, `jobs/worker.py` and
`jobs/policy.py`:

- **One active run per kind**, enforced by the database, not by application
  bookkeeping. A trigger for a kind that already has a pending or in-progress
  run *coalesces* onto it rather than queueing a duplicate — repeated manual
  refreshes cannot inflate history.
- **Leases with heartbeats.** A claimed run heartbeats every 60 s from a
  background task; a lease stale for 5 minutes is reclaimable, so a crashed
  worker does not wedge a kind forever.
- **Bounded retry.** Exponential backoff from 30 s, doubling, capped at 900 s,
  with the failure streak counted over a 24-hour lookback.
- **Shared process-wide rate limiters.** Token buckets for RPC, CoinMarketCap
  and the CoinGecko icon endpoint are shared across all jobs in the worker, so
  three concurrent job kinds cannot collectively trip a provider's 429.
- **Cancellation checkpoints.** Long jobs check for a cancel request between
  bounded batches rather than being killed mid-write.

---

## 5. External providers

Every provider has a **keyless zero-config default**; keys are an upgrade path.

| Provider | Key? | Role |
|---|---|---|
| Public JSON-RPC (`providers/rpc_defaults.py`) | keyless | `ethereum-rpc.publicnode.com`, `cloudflare-eth.com`, `1rpc.io/eth`. Always appended after whatever is configured. |
| Owner-configured JSON-RPC | optional | Stored encrypted as integration kind `rpc`. Read-only methods only. |
| CoinMarketCap public API | keyless | The default quote provider on a fresh install. Accepts only numeric ids, hence the `cmc_map_*` resolution tables. |
| CoinGecko Demo API | **key required** | Quotes when a key is stored; also the only news source. |
| Trust Wallet GitHub assets | keyless | Primary token-icon source, addressed by EIP-55 checksummed address. |
| CoinGecko contract lookup | keyless | Icon fallback, behind its own rate limiter. |
| Vendored catalogs | n/a | `uniswap_mainnet_tokenlist.json` and `cmc_map_seed.json` ship in the image so boot never depends on an upstream commit. |

### RPC failover

`RpcReader` moves to the next endpoint whenever one reports itself unusable:
HTTP 402 (plan exhausted), 401/403 (bad or revoked key), 5xx, a transport
failure, or a 429 that outlived its retries. The endpoint that answers is sticky
for the rest of that job run; the next run starts from the configured endpoint
again, so a keyed provider recovers by itself once its quota resets.

Two deliberate exceptions:

- **`validate_rpc` never falls back.** It probes exactly the endpoint you
  configured, so Settings → Integrations keeps telling the truth about *your*
  key rather than about a public fallback.
- **A URL that stops passing SSRF validation is a hard failure**, not a reason
  to fall back. Silently rerouting a rebound hostname would defeat the check.

`providers/rpc_targets.py` performs that validation: DNS resolution with
rebinding checks, denial of link-local, cloud-metadata and multicast
destinations, refusal of redirects, and an explicit owner opt-in
(`allow_private_host`) for private nodes. Quote provider origins are fixed by
their adapter — there is no arbitrary-proxy setting.

### Quote provider selection

`jobs/quotes.get_active_quote_provider()` returns `"coingecko"` when an API key
is stored and `"coinmarketcap"` otherwise. Only **held** assets are priced, so
cost scales with the portfolio rather than with the catalog.

---

## 6. Authentication, sessions and secrets

Single owner, server-side opaque sessions. No JWT.

**Setup.** `POST /api/v1/setup` hashes the password with Argon2id
(`time_cost=2, memory_cost=65536, parallelism=2`, executed on a worker thread)
into the singleton `owner` row. Concurrency is resolved by the database: a
second insert hits the partial unique index and becomes a 409, so two
simultaneous setup requests cannot both win.

**Login.** Verifies the hash, transparently rehashes if the parameters changed,
records a `login_attempt`, and creates a `session` row with a fresh
`secrets.token_hex(32)` CSRF token and a 24-hour TTL.

**Throttle.** Ten failed attempts within 15 minutes yields HTTP 429 with
`Retry-After: 900`. Attempts are rows in the database, so the cooldown survives
a process restart.

**Session cookie.** `sid`, `httponly`, `samesite=lax`, `path=/`,
`max_age=86400`, and **`secure=False`** — the stack serves plain HTTP
internally and TLS is terminated by a proxy in front of it. If you expose audr
beyond localhost, that proxy is not optional.

**CSRF.** Every mutating route requires, in addition to a valid session: an
`Origin` whose netloc equals the `Host` header when `Origin` is present, and an
`X-CSRF-Token` header matching `session.csrf_token`. The token is returned in
the response bodies of `/setup`, `/auth/login` and `/auth/session` — this is
double-submit via header, not via a second cookie.

**Revocation.** Logout revokes one session. A password change revokes *every*
session and clears the cookie. Expired sessions and old login attempts are
pruned by the worker each cycle.

### Encryption at rest

Two layers:

1. **`SECRET_KEY`** (64 hex characters = 32 bytes) is the key-encryption key.
   It lives only in the environment, never in the database.
2. A 32-byte **master key** is generated on first boot and stored in `key_state`
   wrapped by the KEK. Provider credentials in `integration.encrypted_blob` are
   sealed with it using a versioned AES-256-GCM envelope
   (`version | nonce | ciphertext+tag`).

This covers credentials only. Wallet addresses, balances and valuation history
are plaintext columns — see [security-at-rest.md](security-at-rest.md) for the
threat model and the volume-encryption recommendation. Losing `SECRET_KEY`
means the application refuses to start and every stored credential is
unreadable; recovery is in
[operations.md](operations.md#key-loss-behavior).

---

## 7. Frontend

A React 19 SPA built by Vite and served as static files by the `api` process
from `/app/static` (AUD-388; nginx did this before). No server-side rendering
and no router library.

- `main.tsx` → `App.tsx`. TanStack Query drives everything. The app gates on
  `fetchSetupStatus` then `fetchSession`, rendering `SetupPage` or `SignInPage`
  when unauthenticated and the `Layout` shell otherwise. A global 401 handler
  registered via `setUnauthorizedCallback` bounces an expired session back to
  sign-in from anywhere.
- `routing.ts` implements **hash-based routing** — `#/wallets?period=30d`, where
  the path segment selects the page and the query segment holds per-screen view
  state. Hash routing was chosen so the server needs no per-route rewrite rules
  beyond the single SPA fallback on `/`.
- `src/pages/` — Dashboard, Holdings, Wallets, Assets, Events, Connections,
  History, Schedules, Status, AccountData, Assistant, Setup, SignIn.
- `src/components/` — Layout (which owns the nav list), AllocationList,
  AssetEmblem, AssistantPanel, ErrorBoundary, HistoryChart, HistoryTable, Icons,
  MoneyValue, NewsFeed, ScanStatus.
- `src/api/client.ts` — `BASE = '/api/v1'`, a module-level CSRF token
  (`setCSRFToken`/`getCSRFToken`), and `ApiError`/`AuthError` classes over the
  typed error envelope.
- One stylesheet, `src/styles/folio.css`. `decimal.js` keeps client-side money
  formatting exact; Recharts draws the history chart, which also exposes an
  accessible data-table alternative.

---

## 8. Configuration reference

Every environment variable the backend reads, from `config.py`. `DATABASE_URL`
and `SECRET_KEY` are also read directly by the `python -m audr.operations.init_key`
CLI path.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | **required** | Must use the `postgresql+psycopg://` scheme; validated at startup. |
| `SECRET_KEY` | **required** | The KEK. Exactly 64 hex characters. |
| `DEBUG` | `false` | Passed to `create_async_engine(echo=...)`. |
| `LOG_LEVEL` | `INFO` | |
| `RPC_RATE_LIMIT_PER_SECOND` | `10.0` | Worker-wide shared RPC token bucket. Conservative for a free Infura tier. |
| `RPC_RATE_LIMIT_BURST` | `5` | |
| `EVENT_INDEXER_MAX_CHUNKS_PER_RUN` | `50` | Caps `eth_getLogs` chunks per run; the remainder resumes from the checkpoint. |
| `CMC_RATE_LIMIT_PER_SECOND` | `0.5` | Keyless CoinMarketCap quota is tight and unpublished. |
| `CMC_RATE_LIMIT_BURST` | `1` | |
| `ASSET_ICONS_REMOTE_FETCH` | `true` | Set `false` to stop all outbound icon fetches; the UI then shows monograms only. |
| `ASSET_ICON_CG_RATE_LIMIT_PER_SECOND` | `0.5` | Keyless CoinGecko icon fallback. |
| `ASSET_ICON_CG_RATE_LIMIT_BURST` | `1` | |

Test-only variables (`TEST_DATABASE_URL`, `RPC_MOCK_URL`, `QUOTE_MOCK_URL`,
`MASTER_KEY_HEX`) are set by `compose.test.yaml` and read by the test fixtures,
not by `Settings`.

Everything else — schedules, intervals, freshness thresholds, provider budgets,
RPC batch sizes, credentials — is **runtime configuration stored in the
database and edited through the web UI**. That is a constitutional requirement,
not a convenience: routine operation must not require editing a file or opening
a shell.

---

## 9. Design rules worth preserving

These are the invariants the rest of the system is built on. Breaking one is a
correctness bug, not a style disagreement.

1. **Unknown is never zero.** A failed read, a missing price and a genuine zero
   balance are three different states with three different representations.
   `total_usd` is null when any included holding lacks a usable input; the
   priced subtotal is reported separately and labelled incomplete.
2. **Decimal, never float.** `numeric` in the database, `Decimal` in Python,
   `decimal.js` in the browser, exact strings on the wire. Financial values are
   JSON strings, never JSON numbers.
3. **Assets are identified by chain plus contract address**, never by symbol.
   Native ETH has its own sentinel identity.
4. **Published rows are immutable.** Corrections are new rows plus invalidation
   records. Reorgs invalidate, they do not rewrite.
5. **Repetition does not accumulate.** Duplicate addresses, repeated scans,
   coalesced triggers and retries must not inflate balances or history —
   enforced by the active-run index and the snapshot `input_key`.
6. **Freshness facts are separate.** Balance freshness, quote freshness and read
   time are independently tracked and independently surfaced.
7. **Credentials never leave the envelope.** Not in responses, not in logs, not
   in exports, not in error messages.
8. **Keyless by default.** Every provider works with no configuration; keys are
   an upgrade.
