# Quickstart and Validation Guide

Status: implementation contract, not an available application.
The commands below become runnable when the corresponding Docker files, scripts,
test fixtures and package entry points are implemented in the tasks phase.
They have not been executed against a built application.

## Prerequisites and deployment

Reference host: Linux amd64, 4 vCPU, 8 GiB RAM, SSD; Docker Engine with Compose v2.
For live use, supply an Ethereum mainnet RPC endpoint supporting safe block reads.
A CoinGecko Demo key is optional for quantities and required for that adapter's USD quotes.
Deterministic testing uses controlled services and needs no personal provider credentials.

Planned deployment commands, from project root:

```sh
docker compose build
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8080/health/ready
```

Expected: DB becomes healthy; init establishes the key; migration exits successfully;
API and worker run. The API binds only to loopback by default. Open
http://127.0.0.1:8080 locally (or through an SSH tunnel) and create the owner password.
For remote access, follow the future docs/operations.md TLS/trusted-origin setup.
Do not publish an unclaimed setup screen to the internet.

Through the browser: configure and validate RPC, optionally configure quotes, add labeled
wallets, inspect native/manual holdings and catalog discovery progress. Adjust all three
polling schedules and budgets in settings. Check that the network selector offers Ethereum
only, the UI is English, and valuation currency is USD.

## Deterministic automated suite

The test Compose file must isolate DB/key volumes and expose controlled RPC/quote fixtures
only on its private network. Tests must never reuse production data or credentials.

```sh
docker compose -f compose.test.yaml run --rm backend-tests
docker compose -f compose.test.yaml run --rm frontend-tests
docker compose -f compose.test.yaml run --rm e2e-tests
docker compose -f compose.test.yaml run --rm benchmark
```

Expected entry points:

- backend-tests: pytest unit/integration/contract suites, actual PostgreSQL, controlled
  RPC and quote HTTP behavior, migrations and cryptographic envelope checks.
- frontend-tests: Vitest calculations/formatting and interaction states.
- e2e-tests: Playwright setup/settings/wallet/dashboard/history journeys.
- benchmark: seed reference data, execute 100 warm dashboard/history trials and report
  p50/p95, peak memory, row counts, fixture identity and host specification.

Container exit status must be nonzero on any required failure. Test reports must state
that fixture success does not verify third-party availability.

## Scenario 1 — Single owner and secrets

Run two setup requests concurrently: exactly one succeeds; the other returns 409.
Unauthenticated portfolio/settings/export requests return 401. Login establishes an
opaque HttpOnly session; expired/revoked sessions are rejected. Mutation without valid
CSRF/origin fails. Repeated bad passwords produce the specified cooldown across restart.

Change the password in the UI: all sessions are revoked and the new password works.
Scan logs, ordinary exports and saved settings responses for fixture secret markers;
none may appear. Verify an encrypted DB copy cannot expose RPC keys or quote values
without the separate master key. The whole RPC URL, including path/query credentials,
must be protected. These checks support SC-007.

## Scenario 2 — RPC catalog and exact balances

Fixture A contains ETH, a catalog token with 6 decimals, a token with 18 decimals and
an unknown contract. Fixture B shares a held token and includes a zero-decimal token.
Include two different contracts with the same symbol and both tiny and uint256-boundary
raw amounts. Compare exact results to independently calculated expected quantities.

Full discovery finds nonzero catalog holdings. The unknown contract remains outside
coverage until manually added through the UI; subsequent RPC refresh includes it.
Adding the same address with different case does not create another wallet.
Adding a catalog contract manually does not double-count it.
Add a valid public address without an ownership proof; the UI calls it tracked rather
than claiming the owner controls it, and no signing request occurs.

Simulate a wrong chain, malformed/reordered/missing batch replies, contract revert,
missing decimals and catalog/onchain metadata conflict. Verify per-item errors and
raw-unit display without an assumed 18 decimals. No signature or send-transaction
method may reach the fixture RPC. These checks support US1 and SC-002/003.

## Scenario 3 — Prices, allocations and budgets

Fixture quote responses provide exact decimal values, missing tokens, an outdated
timestamp, a future timestamp, a rate limit and a server outage.
Verify decimal totals, source attribution, timestamps, unpriced counts and allocation
denominator. ETH uses its native ID rather than a WETH contract; no symbol-only mapping.
Verify requests contain contract/asset IDs but no wallet addresses or owned amounts.

Without a price key, holdings remain usable. Invalid keys pause quotes without stopping
RPC scans. Repeated manual requests share the rate/monthly budget and do not cause
duplicate quote jobs. Shortening the interval updates the usage projection.
Exclusions affect current totals while retaining the excluded view and prior history.
With only excluded nonzero holdings and successful observations for all active wallets,
the included total is zero with no allocation chart and a visible excluded-only reason.
A never-successful active wallet or unknown included input keeps the total unknown.

## Scenario 4 — Schedules, restart and reorg

Configure balance/price schedules to 60 seconds in the controlled test environment,
discovery to its 900-second minimum; fake time advances without waiting in real time.
Pause a schedule and verify no new scheduled work. An active run finishes unless cancelled.
Burst manual refresh calls coalesce. Discovery yields so other jobs can progress.

Stop the test worker during a chunk, restart it and expire the old lease. A late result
from the old worker must not publish. Recover checkpoints without duplicate observations.
After simulated downtime, at most one missed occurrence per kind is queued.
If scope changes during a balance run, discard its aggregate and schedule a fresh run.
Read the same raw quantity at a later verified block: its block time refreshes balance
freshness while the separate read time remains visible. A failed read retains the older
successful block time; retrying the same observation creates no duplicate history.

Change the pinned safe-block hash before publication: no fresh snapshot is committed.
Invalidate an already recorded unfinalized block: current state and affected chart points
are marked invalid; a new observation restores current state. Failure to verify history
is verification_pending, not confirmed. These checks support SC-005.

## Scenario 5 — Historical integrity and browser behavior

Record multiple observations with changed prices and balances. Introduce an outage,
stop/reactivate a wallet and change an exclusion. Verify recorded values and scope
markers survive; no values appear before tracking and gaps are not zero-filled.
Price-only observations retain original balance timestamps.

Inspect desktop 1440px and mobile 390px with keyboard navigation. Totals, asset lists,
charts and forms remain understandable. Text/table equivalents expose chart information.
No-wallet, zero-value and unpriced states avoid fabricated pie charts.

Seed 50 wallets, 100 held identities, 500 nonzero wallet/asset pairs and 8,760 hourly
snapshots. At least 95/100 warm local-network trials must complete within three seconds.
Measure catalog scan call counts separately with 5,000 synthetic entries and record
provider pacing. Do not reuse UI latency as a scan speed claim. Supports SC-004/008.

## Scenario 6 — Export and restart persistence

Browser exports: reconcile fixture wallets, quantities, historical values, times and
coverage with source records. Inspect both JSON and CSV schema-version/record-type fields,
quote status and source times, membership/exclusion revisions, unknown-versus-zero values
and snapshot provenance. Verify secrets/session tokens are absent and history is not
limited to chart samples. Treat returned provider-derived data as private owner exports.

Restart API, worker and database with persistent volumes intact. Verify all committed
wallets, settings, observations and encrypted provider credentials remain readable,
and unfinished jobs resume without duplicate publication.

Test missing/wrong key in an isolated fixture: visible unavailable state, no silent key
regeneration. Test migration failure: API/worker stay unready and committed data is not
destructively reset. The planned password-reset command uses hidden interactive input:

```sh
docker compose exec api python -m audr.operations.reset_password
```

It changes the password and revokes sessions without touching the encryption key.
Provider-data purge requires preview/confirmation and preserves onchain records.
These checks support SC-006/007. Full backup/restore procedures and tests are backlog.

## Optional live smoke check

After fixture tests pass, an owner may validate their configured RPC and Demo key using
the web UI and manually refresh a chosen public wallet. Record elapsed time, logical calls,
provider errors, catalog coverage and quote freshness. Redact identifiers in shared reports.
Do not promise catalog completeness or price availability from a single successful scan.
Do not run repeated live load tests or enable paid providers as part of automated checks.

## Planning-time validation already possible

Verify links between spec/plan/research/data model/contracts, every FR/SC mapping, no
unresolved placeholders and all required artifact paths. Use the installed Spec Kit
core-pack Bash check-prerequisites.sh --json when generated Python helpers are absent.
This checks artifact presence; it does not execute the application or validate behavior.
