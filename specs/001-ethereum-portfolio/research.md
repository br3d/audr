# Research: Ethereum Portfolio

Date: 2026-09-25. Decisions apply only to release 1. Provider capabilities and quotas
are research observations, not service guarantees. No subscriptions were purchased,
credentials used, live wallet scans run or performance claims verified.

## R1. Application structure and stack

**Decision**: Python 3.14, FastAPI, SQLAlchemy 2.0, Alembic and PostgreSQL 18;
React 19, TypeScript and Vite 8 for a same-origin SPA. Node 24 is the build runtime.
Use HTTPX for explicit read-only RPC/quote adapters, eth-utils/eth-abi for address and
ABI handling, cryptography for authenticated encryption and argon2-cffi for password
hashing. Use pytest, Vitest and Playwright. Lock exact compatible patch versions during
implementation; selecting version families here is not a claim that dependencies are installed.

**Rationale**: Python provides integer/decimal arithmetic and convenient future AI
integration; PostgreSQL provides exact numeric storage, transactional history and durable
job claims. Compile the SPA into the application image; use one API process and one worker
process from that image. SQLAlchemy 2.0 is a deliberate established baseline even though
2.1 has just been released.

**Alternatives**: TypeScript end-to-end is viable but requires equally deliberate decimal
handling. SQLite reduces deployment footprint but complicates concurrent durable writes.
Redis/Celery and microservices add operational services without a release-1 requirement.

Sources: [Python](https://www.python.org/getit/),
[FastAPI containers](https://fastapi.tiangolo.com/deployment/docker/),
[SQLAlchemy releases](https://www.sqlalchemy.org/changelog/),
[PostgreSQL versions](https://www.postgresql.org/support/versioning/),
[React versions](https://react.dev/versions), [Vite releases](https://vite.dev/releases),
[Node release schedule](https://nodejs.org/en/about/previous-releases).

Frontend choices: TanStack Query 5 for server-state fetching, Recharts for responsive
charts with explicit textual alternatives, decimal.js for exact display formatting.
Sources: [TanStack Query](https://tanstack.com/query/latest/docs/framework/react/overview),
[Recharts](https://recharts.github.io/en-US/guide/),
[decimal.js](https://mikemcl.github.io/decimal.js/).

## R2. Catalog and discovery

**Decision**: Bundle a reviewed Ethereum-only snapshot of ethereum-lists/tokens, preserving
MIT attribution and recording the upstream commit, source path, content hash and count.
Select and pin the exact upstream commit while building the first artifact. Runtime scans
use the bundled catalog; updating the application supplies a newer reviewed version.
Manual contracts survive catalog replacement. Keep previously discovered wallet/contract
pairs monitored even if a newer catalog removes the contract.

**Rationale**: This is the catalog family used by the user's pyetherbalance reference.
It supplies candidates without requiring a live catalog service or indexer. Membership
does not certify safety or completeness. Reject malformed/duplicate entries during build;
do not download token logos or execute metadata-provided URLs.

**Alternatives**: Transfer-log indexing is explicitly excluded. A proprietary indexer
is optional future work. Uniswap's default list has different licensing from the MIT
Token Lists schema; do not assume those licenses are interchangeable.

Sources: [Ethereum Lists](https://github.com/ethereum-lists/tokens),
[license](https://github.com/ethereum-lists/tokens/blob/master/LICENSE),
[PyEtherBalance](https://pypi.org/project/pyetherbalance/),
[Uniswap default list](https://github.com/Uniswap/default-token-list).

## R3. RPC access and observation consistency

**Decision**: Validate eth_chainId=1. Read ETH with eth_getBalance and ERC-20 raw units
with eth_call(balanceOf). Resolve a safe block once per balance job; prefer block-hash
calls with requireCanonical, otherwise use a fixed block number and verify its hash
before commit. A missing safe tag is a visible capability error, not silent latest fallback.
No wallet keys, signing or gas expenditure.

Catalog discovery is a separate, resumable job. Each wallet's discovery batch pins its
own safe block and records discovery time; discoveries enqueue a coherent balance job.
Discovery results identify monitored pairs but do not become a supposedly simultaneous
portfolio snapshot. A balance job reads all monitored pairs plus manual contracts and
ETH at one pinned block. Discovery freshness remains visible alongside balance freshness.

Use transport batches of 50 calls, up to two concurrent batches and a shared default
20 logical calls/second ceiling. These are application settings, not provider limits.
On batch rejection reduce batch size and then use individual calls; match results by ID.
An explicit provider read error remains an error, even when other calls succeed.

**Rationale**: Batching reduces round trips, but billing can count each method.
Safe blocks reduce ordinary reorg exposure without claiming finality.
Recheck recorded non-finalized observation hashes as finality advances; invalidate
dependent valuations on mismatch and obtain a new current observation.

**Alternatives**: Moving latest per read would mix blocks invisibly. Finalized-only reads
are older; live event subscriptions and historical log scans are unnecessary in release 1.

Sources: [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/),
[EIP-1898](https://eips.ethereum.org/EIPS/eip-1898),
[Geth batching](https://geth.ethereum.org/docs/interacting-with-geth/rpc/batch).

## R4. Metadata, money and history

**Decision**: Store raw units as NUMERIC(78,0) with uint256 bounds. Decimals must be an
integer 0–255; ETH uses 18. ERC-20 metadata methods are optional. Use onchain decimals
when valid, otherwise validated catalog decimals with a provenance label. A conflict
requires an owner-confirmed override through settings; a manually added token without
decimals remains visible in raw units and unpriced until supplied.

Parse numeric quote tokens directly as Decimal. Reject non-finite, negative or malformed
prices; enforce a generous documented input precision bound (128 significant digits,
absolute exponent <=308). Use Decimal context precision 1024 with traps for inexact
accounting arithmetic; this covers accepted uint256/decimal inputs and aggregation at
the reference scale. Round only display values, using ROUND_HALF_EVEN for USD cents.
Send financial values as JSON strings. Chart pixels may use approximate numbers after
normalization; labels, totals and tooltips use authoritative strings.

Persist immutable valuation snapshots referencing balance observations, quote versions,
membership/exclusion revisions and completeness. Persist an invalidation record for a
reorg instead of rewriting old facts. Keep chart summaries indexed by time; decrypt selected
summaries without scanning every historical holding. Never backfill a later quote as if
it had been known at an earlier observation time.

**Alternatives**: Floats, fixed two-decimal prices and symbol matching lose meaning.
Recomputing history from current memberships/prices silently changes the owner's records.

Sources: [ERC-20](https://eips.ethereum.org/EIPS/eip-20),
[PostgreSQL numeric](https://www.postgresql.org/docs/18/datatype-numeric.html),
[Python Decimal](https://docs.python.org/3/library/decimal.html).

## R5. USD quotes and budget

**Decision**: Implement CoinGecko Demo as the first optional quote adapter. The owner
enters a Demo key in settings; absent quotes do not prevent balance tracking.
ETH uses simple/price with ids=ethereum; contracts use simple/token_price/ethereum.
Request USD, full precision and last_updated_at. Query only distinct nonzero held assets,
deduplicated across wallets; use 100 contracts per batch plus one ETH request.
Do not equate ETH and WETH or rely on symbols.

Published Demo limits at research time are 10,000 monthly credits and 100 requests/minute.
Use a conservative application ceiling of 30 attempts/minute and configurable local
8,000-attempt monthly budget, including failed attempts and retries for safety.
Local usage is an estimate when a key is also used elsewhere.
Default 15-minute polling with ETH and up to 100 tokens costs about 5,760 requests/30 days,
before retries/manual refresh. Five-minute polling would need about 17,280.
Formula: days × 1440 / interval_minutes × (ETH_present + ceil(token_count / 100)).
No automatic paid upgrade or credential acquisition.

Sources: [Demo pricing](https://www.coingecko.com/en/api/pricing),
[token quotes](https://docs.coingecko.com/demo/reference/simple-token-price),
[native quotes](https://docs.coingecko.com/demo/reference/simple-price),
[usage accounting](https://support.coingecko.com/hc/en-us/articles/13962109374489-How-does-CoinGecko-API-count-the-API-usage-credit).

**Storage decision**: Keep provider quotes and derived USD summary payloads encrypted
with versioned application AEAD, apart from public onchain quantities. Refresh the current
quote cache on the configured schedule; distinguish immutable historical observations from
current cache. Attribute CoinGecko in valuation views. Its terms contain storage and
termination/deletion conditions; indefinite retention irrespective of provider rights is
not promised. Track provenance for owner-requested removal of provider-derived records,
including any copies independently retained by the owner. Onchain observations remain independent.
Disabling polling is not itself treated as provider termination. No data is automatically
deleted based on an API error. Recheck applicable terms before distribution.

Sources: [API terms, sections 4, 6 and 10](https://www.coingecko.com/en/api_terms),
[attribution](https://brand.coingecko.com/resources/attribution-guide).

**Alternatives**: DefiLlama has an official keyless price API but lacks a numeric public
rate guarantee in its overview and has data-use restrictions of its own. Keep it a future
adapter, not a silent fallback. Onchain oracle/DEX pricing requires coverage, liquidity and
manipulation policies beyond this release.
Sources: [DefiLlama API](https://api-docs.defillama.com/llms-free.txt),
[DefiLlama terms](https://defillama.com/terms).

## R6. Durable work and freshness

**Decision**: PostgreSQL jobs and a single worker, with row claims, lease tokens,
heartbeat and transactional publication. RPC calls happen outside database transactions.
Initial application defaults, editable in the UI: balance refresh 15 minutes, full catalog
discovery 24 hours, quotes 15 minutes. Minimum intervals: balances/quotes 60 seconds,
discovery 15 minutes; all subject to owner/provider budgets. Manual work uses the same limits.

Initial discovery runs after adding a wallet; quick refresh scans known/manual holdings.
UI actions distinguish Refresh balances and Discover tokens. Discovery yields between
batches to short balance/quote jobs. Coalesce missed schedule occurrences after downtime.
Retry transient calls at most three attempts with jitter (1s, 4s), honoring Retry-After;
defer the job when server cooldown exceeds the attempt window. Invalid credentials pause
the integration until edited. Balance jobs have a 10-minute observation window;
expired jobs publish no fresh aggregate and restart at a new block on the next eligible run.
Long discovery jobs checkpoint cursors but restart an expired wallet's block-bound batch.

Freshness defaults: twice the selected balance/quote interval with a 5-minute floor;
full discovery becomes overdue at twice its interval. Paused polling still ages observations.
For stale quotes retain a labeled last-known estimate. Explicitly missing quotes remain
unpriced. Partial balances preserve original timestamps and mark mixed observations.

**Alternatives**: In-process web timers stop on API restart; memory-only queues lose work.
Unbounded retries hide cost and can overload providers.
Source: [PostgreSQL queue-related locking](https://www.postgresql.org/docs/18/sql-select.html).

## R7. Authentication, secrets and recovery

**Decision**: Single owner, Argon2id password hash (64 MiB, 3 iterations, parallelism 1),
12–128 character passwords with paste support and no forced composition rules.
Database-backed opaque sessions: 32 random bytes, only digests stored; 12-hour absolute
and 1-hour idle expiry. HttpOnly/SameSite=Lax cookies, Secure in HTTPS mode; enforce
same-origin and CSRF token checks on mutations. Rotate on login; revoke all on password
change/reset. Apply persistent IP and account throttles; never log submitted passwords.

Default deployment binds to loopback, so first setup occurs locally or through an SSH
tunnel. No public unclaimed-owner form. Remote access uses a documented TLS reverse proxy
with explicit trusted origin/proxy settings. Provider endpoint changes are authenticated,
allow only HTTP(S), block metadata/link-local targets and redirects; private-node access
requires explicit confirmation of the target host in settings.

Store credentials and sensitive monetary payloads using AES-256-GCM with fresh nonces,
record-bound associated data and key versions. A one-shot init service creates a master
key on a restricted persistent key volume only for an empty installation. API and worker
mount it read-only. Missing existing keys cause recovery mode; never replace silently.
The key protects a copied DB, not a compromised running host.

Full backup/restore was deferred to backlog by the owner during clarification.
Release 1 verifies persistent DB/key volumes across ordinary restarts and reports missing
keys without replacing them. Password reset is an exceptional operator command with
hidden input, not a public endpoint; it preserves keys.

Sources: [OWASP passwords](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[OWASP sessions](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html),
[AEAD](https://cryptography.io/en/latest/hazmat/primitives/aead/),
[Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/),
[PostgreSQL backup](https://www.postgresql.org/docs/18/backup-dump.html).

## R8. Reference scale and release gates

**Decision**: Benchmark on Linux amd64, 4 vCPU, 8 GiB RAM, SSD and local-network browser.
Use 50 wallets, 100 distinct held asset identities, 500 nonzero wallet/asset pairs and
8,760 hourly snapshots: about 4.38 million historical holding references. Test catalog
work separately with a 5,000-entry synthetic catalog (250,000 wallet/token checks).
These are engineering fixtures, not hard product limits or live-provider timing promises.
Keep an indexed timestamp summary path with at most 2,000 chart points; select actual
observations and preserve gaps/scope markers without deleting underlying history.

**Rationale**: Dashboard latency depends on stored data access, while scan latency depends
on catalog size and provider budget. Keeping these measurements separate makes SC-004 honest.
Test inexact numbers, partial responses, reordered batches, reorgs, lease loss, restoration,
CSRF and concurrent setup in deterministic fixtures; use optional real-source smoke checks.

## R9. Spec Kit setup

**Decision**: Resolve plan-template with the installed CLI. The generated skill contains
a literal SCRIPT placeholder and the project lacks Python helper scripts; the installed
CLI core pack supplies the official Bash equivalents. The bundled setup-plan.sh was read
and run from this project; it resolved feature.json correctly. Its BRANCH output is a
feature identifier fallback, not evidence that a Git branch exists. No Git state changed.
The same core-pack check-prerequisites.sh can support the next tasks phase.

No architectural questions remain unresolved for this plan. Exact patch locks, catalog
commit selection, fixture benchmarks and real-source compatibility are implementation
verification steps, not evidence already obtained.
