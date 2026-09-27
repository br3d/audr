# Tasks: Ethereum Portfolio

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), and [quickstart.md](quickstart.md).

**Tests**: Required by the constitution and the specification. Write each story's listed automated checks before its implementation and establish a failing result first. Use deterministic fixtures; optional live-provider checks need owner-supplied credentials.

**Organization**: Tasks are grouped by user story. Paths are relative to the repository root. `[P]` means the task can proceed in parallel with adjacent tasks after its stated prerequisites are met; it does not waive phase or file dependencies.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the planned single-image Python API/worker and same-origin React application without production credentials.

- [ ] T001 Initialize the Python 3.14 backend with pinned compatible FastAPI, SQLAlchemy, Alembic, psycopg, HTTPX, eth-utils/eth-abi, cryptography, argon2-cffi and test dependencies in `backend/pyproject.toml` and `backend/uv.lock`.
- [ ] T002 [P] Initialize the React 19/TypeScript/Vite 8 frontend with pinned TanStack Query, Recharts, decimal.js, Vitest and Playwright dependencies in `frontend/package.json` and `frontend/package-lock.json`.
- [ ] T003 Configure backend linting, type checks and pytest markers for unit, integration and contract checks in `backend/pyproject.toml` and `backend/tests/conftest.py`.
- [ ] T004 [P] Configure frontend TypeScript, Vitest and Vite test/build scripts in `frontend/tsconfig.json`, `frontend/vite.config.ts` and `frontend/vitest.config.ts`.
- [ ] T005 Create the multi-stage frontend/backend application image with pinned base-image digests and non-root runtime in `Dockerfile`.
- [ ] T006 Define `db`, one-shot `init`, one-shot `migrate`, `api` and `worker` services, persistent DB/key volumes, health ordering and loopback-only API binding in `compose.yaml`.
- [ ] T007 [P] Define isolated test services and volumes for PostgreSQL, controlled providers, backend tests, frontend tests, e2e tests and benchmark in `compose.test.yaml`.
- [ ] T008 [P] Add repository ignore rules for credentials, local volumes, build output and test artifacts in `.gitignore`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Establish persistence, encryption, safe API behavior and durable work shared by all four stories.

**Checkpoint**: Complete this phase before beginning any user-story implementation.

- [ ] T009 Add environment parsing, startup validation and database session management in `backend/src/audr/config.py` and `backend/src/audr/db.py`.
- [ ] T010 Initialize SQLAlchemy metadata and Alembic with transactional migration configuration in `backend/src/audr/models.py`, `backend/alembic.ini` and `backend/migrations/env.py`.
- [ ] T011 [P] Add failing tests for AEAD envelope versioning, record-bound associated data, missing-key recovery and secret redaction in `backend/tests/unit/test_crypto.py` and `backend/tests/integration/test_key_lifecycle.py`.
- [ ] T012 Implement one-shot master-key initialization, existing DB/key-pair validation and missing-key failure in `backend/src/audr/operations/init_key.py`.
- [ ] T013 Implement versioned AES-256-GCM encryption/decryption for credentials and provider-derived monetary payloads in `backend/src/audr/operations/crypto.py`.
- [ ] T014 [P] Add request IDs, sanitized API errors, private-route `Cache-Control: no-store` and redacted structured logging in `backend/src/audr/api/app.py` and `backend/src/audr/api/errors.py`.
- [ ] T015 [P] Add controlled Ethereum RPC and quote HTTP fixtures, fake clock and PostgreSQL test database setup in `backend/tests/fixtures/rpc.py`, `backend/tests/fixtures/quotes.py` and `backend/tests/conftest.py`.
- [ ] T016 Add failing lease-loss, duplicate-claim, retry-budget and restart-recovery tests in `backend/tests/integration/test_worker_leases.py`.
- [ ] T017 Create `job_run`, `schedule`, `provider_budget`, `worker_status` and `operational_event` tables with constraints/indexes from the data model in `backend/migrations/versions/001_foundation.py`.
- [ ] T018 Implement PostgreSQL job claim/fencing, one-active-run-per-kind, heartbeats, checkpointed recovery and transactional budget reservation in `backend/src/audr/jobs/store.py` and `backend/src/audr/jobs/worker.py`.
- [ ] T019 Implement bounded retries, shared RPC rate limiting, cancellation checkpoints and coalesced manual work in `backend/src/audr/jobs/policy.py` and `backend/src/audr/jobs/scheduler.py`.
- [ ] T020 Expose secret-free liveness/readiness and authenticated status primitives, including migration/key/worker degradation, in `backend/src/audr/api/health.py` and `backend/src/audr/operations/status.py`.

---

## Phase 3: User Story 1 — Set up and inspect wallet holdings (Priority: P1) 🎯 MVP

**Goal**: One owner configures Ethereum RPC, tracks public addresses and sees exact ETH and catalog/manual ERC-20 quantities without requiring a quote key.

**Independent Test**: With controlled RPC and two fixture wallets, complete first-run setup and inspect native/catalog/manual holdings, duplicate handling, scan coverage and failure statuses while quotes remain unconfigured.

### Required tests — write failing checks first

- [ ] T021 [P] [US1] Add concurrent setup, authentication, session expiry, CSRF/origin, password-change and persistent login-throttle tests in `backend/tests/integration/test_auth.py`.
- [ ] T022 [P] [US1] Add RPC contract fixtures for wrong chain, safe-block identity, reordered/missing batch IDs, reverts, malformed values and no signing methods in `backend/tests/contract/test_rpc_reader.py`.
- [ ] T023 [P] [US1] Add catalog/manual discovery, zero/nonzero, duplicate-contract, partial-coverage and restart-checkpoint tests in `backend/tests/integration/test_discovery.py`.
- [ ] T024 [P] [US1] Add exact raw-unit, address-case deduplication, arbitrary valid public-address tracking without ownership proof, unknown-versus-zero, metadata-conflict and per-item failure tests in `backend/tests/integration/test_holdings.py`.
- [ ] T025 [P] [US1] Add setup, login, RPC connection, public-address addition without an ownership claim, and holdings browser journeys using controlled providers in `tests/e2e/holdings.spec.ts`.

### Implementation

- [ ] T026 [US1] Create owner, session, throttle and encrypted integration tables with singleton/uniqueness constraints in `backend/migrations/versions/002_owner_integrations.py` and models in `backend/src/audr/auth/models.py`.
- [ ] T027 [US1] Implement atomic one-owner setup, Argon2id password hashing, opaque database sessions, password change and persistent IP/account throttling in `backend/src/audr/auth/service.py`.
- [ ] T028 [US1] Implement setup/login/session/logout/password routes and same-origin/CSRF enforcement in `backend/src/audr/api/auth.py` and `backend/src/audr/auth/dependencies.py`.
- [ ] T029 [US1] Implement encrypted RPC settings replacement with revision checks, redacted reads and owner-visible disclosure fields in `backend/src/audr/settings/integrations.py` and `backend/src/audr/api/integrations.py`.
- [ ] T030 [US1] Implement HTTP(S) RPC URL validation, DNS/rebinding checks, redirect denial and explicit private-host permission in `backend/src/audr/providers/rpc_targets.py`.
- [ ] T031 [US1] Create wallet, asset, metadata revision, manual token, monitored pair, discovery coverage, catalog version/entry and membership revision/wallet/exclusion tables with chain/contract and case-insensitive address identity in `backend/migrations/versions/003_holdings.py` and `backend/src/audr/wallets/models.py`.
- [ ] T032 [US1] Implement label/add/stop/reactivate handling for any valid public mainnet address without proof of control, and membership revision creation without deleting historical membership, in `backend/src/audr/wallets/service.py`.
- [ ] T033 [US1] Implement authenticated network, wallet, asset, manual-token and catalog routes with pagination and contract error shapes in `backend/src/audr/api/wallets.py` and `backend/src/audr/api/assets.py`.
- [ ] T034 [P] [US1] Pin a reviewed Ethereum-only catalog commit, preserve license/source/hash/count manifest and create the reproducible maintainer refresh script in `assets/token-catalog/manifest.json`, `assets/token-catalog/tokens.json` and `scripts/refresh-catalog.sh`.
- [ ] T035 [US1] Implement validated catalog import/upgrades that retain manual assets and monitored pairs, rejecting malformed or duplicate entries without replacing the working catalog in `backend/src/audr/assets/catalog.py`.
- [ ] T036 [US1] Implement chain-id and safe-block validation, `eth_getBalance`, ERC-20 `balanceOf`, canonical hash checks and robust JSON-RPC batch response matching in `backend/src/audr/providers/rpc_reader.py`.
- [ ] T037 [US1] Implement untrusted ERC-20 metadata reading, catalog fallback, owner-confirmed decimal overrides and raw-only state when decimals are unknown in `backend/src/audr/assets/metadata.py`.
- [ ] T038 [US1] Create discovery candidate, balance set and balance observation tables with raw `NUMERIC(78,0)` and unique publication keys in `backend/migrations/versions/004_observations.py`.
- [ ] T039 [US1] Implement chunked, resumable catalog/manual discovery with per-wallet counts, partial states, monitored-pair deduplication and yielding to short jobs in `backend/src/audr/jobs/discovery.py`.
- [ ] T040 [US1] Implement pinned-block ETH/manual/monitored balance scans, per-item failure persistence, canonical verification and scope/lease-fenced atomic publication; record a new successful observation at a later verified block even for unchanged raw units, retaining block and read times separately, in `backend/src/audr/jobs/balances.py`.
- [ ] T041 [US1] Implement exact wallet/asset quantity aggregation using integer raw units and distinct unknown, zero and stale read states in `backend/src/audr/portfolio/holdings.py`.
- [ ] T042 [US1] Expose authenticated holdings, scan progress and manual balance/discovery triggers without duplicate active runs in `backend/src/audr/api/portfolio.py` and `backend/src/audr/api/jobs.py`.
- [ ] T043 [P] [US1] Build same-origin API client, session/CSRF handling and English setup/sign-in/password screens in `frontend/src/api/client.ts`, `frontend/src/pages/SetupPage.tsx` and `frontend/src/pages/SignInPage.tsx`.
- [ ] T044 [US1] Build RPC connection form, Ethereum-only choice, secret masking and explicit endpoint data disclosure in `frontend/src/pages/ConnectionsPage.tsx`.
- [ ] T045 [US1] Build wallet and asset screens for manual contracts, raw quantities, metadata conflicts, partial catalog coverage and separate refresh/discover actions in `frontend/src/pages/WalletsPage.tsx` and `frontend/src/pages/AssetsPage.tsx`.
- [ ] T046 [US1] Connect the protected holdings flow and source-status messaging in `frontend/src/pages/HoldingsPage.tsx` and `frontend/src/components/ScanStatus.tsx`; run the US1 tests from `backend/tests/` and `tests/e2e/holdings.spec.ts`.

**Checkpoint**: US1 delivers a useful read-only holdings MVP with no price provider, history chart, news, AI or alerts.

---

## Phase 4: User Story 2 — Understand value and allocation (Priority: P1)

**Goal**: Add optional USD quotes, exact portfolio valuation, asset/network allocation, exclusions and honest stale/incomplete labels.

**Independent Test**: Feed saved US1 holdings and controlled quotes into decimal reference fixtures; compare included totals and allocation, then remove prices and fail one wallet refresh to inspect estimate and freshness labels.

### Required tests — write failing checks first

- [ ] T047 [P] [US2] Add decimal calculation, native-versus-contract identity, rounding, tiny/large amounts, excluded-only and unknown-included totals, and allocation-denominator unit tests in `backend/tests/unit/test_valuation.py`.
- [ ] T048 [P] [US2] Add quote-adapter contract tests for absent/stale/future-dated quotes, 429/401 responses, per-asset failures, budgets and no wallet data in requests in `backend/tests/contract/test_quotes.py`.
- [ ] T049 [P] [US2] Add integration tests for last-known balance inclusion without double-counting, stale contribution, estimated complete-input totals, excluded-only zero totals after all active wallets have a successful observation, null totals with unknown included inputs or never-successful wallets, unchanged quantities at a later verified block refreshing balance freshness, separate quote freshness and unknown rather than zero in `backend/tests/integration/test_portfolio_quality.py`.
- [ ] T050 [P] [US2] Add display-format and accessibility tests for decimal strings, empty/unknown charts and textual allocations in `frontend/tests/portfolio.test.tsx`.

### Implementation

- [ ] T051 [US2] Create quote sets/observations and valuation snapshots/lines with input-derived uniqueness and provider provenance, referencing the immutable membership/exclusion revisions created by T031, in `backend/migrations/versions/005_valuation.py` and `backend/src/audr/portfolio/models.py`.
- [ ] T052 [US2] Implement CoinGecko Demo contract/native identity mapping, Decimal parsing, timestamps, encrypted quotes and sanitized source errors in `backend/src/audr/providers/coingecko_demo.py`.
- [ ] T053 [US2] Implement optional quote credentials, fixed provider origin, replacement validation and redacted quote settings in `backend/src/audr/settings/quotes.py` and `backend/src/audr/api/integrations.py`.
- [ ] T054 [US2] Implement held-asset-only quote jobs, per-attempt local budget reservation, bounded retry and independently tracked last-success quotes in `backend/src/audr/jobs/quotes.py`.
- [ ] T055 [US2] Implement uint256-to-quantity conversion and exact Decimal USD line/total calculations, rounding only for display in `backend/src/audr/portfolio/money.py`.
- [ ] T056 [US2] Implement snapshot quality composition from fresh/stale/unknown balances and quotes: sum each usable included holding once in the priced subtotal, expose carried-forward balances as a stale subset, set total null for unknown included inputs or a never-successful active wallet, set an excluded-only included total to zero after every active wallet has a successful observation and included inputs are known, label stale complete-input totals estimated, and retain the priced-subtotal allocation basis in `backend/src/audr/portfolio/valuation.py`.
- [ ] T057 [US2] Publish valuation snapshots transactionally on new verified balance sets, quote sets or scope/exclusion revisions, including a later block with unchanged quantities, deduplicated by exact input key so a retry of the same observation adds no history in `backend/src/audr/jobs/valuation.py`.
- [ ] T058 [US2] Implement asset exclusion/reinclusion and confirmed decimals override without rewriting prior snapshots in `backend/src/audr/assets/service.py` and `backend/src/audr/api/assets.py`.
- [ ] T059 [US2] Return exact decimal-string holdings, `total_usd` only for complete inputs, `priced_subtotal_usd` for known valued holdings, an explained excluded-only zero state, stale contribution, separate balance block/read and quote times, and asset/Ethereum allocation from `GET /portfolio` in `backend/src/audr/api/portfolio.py`.
- [ ] T060 [P] [US2] Implement exact decimal display helpers and textual allocation equivalent in `frontend/src/components/MoneyValue.tsx` and `frontend/src/components/AllocationTable.tsx`.
- [ ] T061 [US2] Build responsive dashboard total, coverage/freshness labels, an excluded-only explanation without an allocation chart, and non-fabricated allocation charts in `frontend/src/pages/DashboardPage.tsx` and `frontend/src/components/AllocationChart.tsx`.
- [ ] T062 [US2] Add quote setup/disclosure, excluded-assets controls and unpriced/metadata-conflict states in `frontend/src/pages/ConnectionsPage.tsx` and `frontend/src/pages/AssetsPage.tsx`.
- [ ] T063 [US2] Add controlled quote/valuation browser journey in `tests/e2e/valuation.spec.ts` and run the US2 checks from `backend/tests/` and `frontend/tests/`.

**Checkpoint**: US2 produces useful USD valuation without presenting missing or stale data as fresh zero.

---

## Phase 5: User Story 3 — Observe portfolio history (Priority: P2)

**Goal**: Preserve and present actual recorded portfolio valuations, gaps, scope changes and reorg invalidation without implying PnL.

**Independent Test**: Record successive controlled observations, price-only changes and membership/exclusion changes; restart storage and compare chart/table points with immutable source records.

### Required tests — write failing checks first

- [ ] T064 [P] [US3] Add snapshot immutability, repeated-publication idempotency, later verified blocks with unchanged quantities, quote-only changes, gap and membership-marker tests in `backend/tests/integration/test_history.py`.
- [ ] T065 [P] [US3] Add reorg and verification-pending tests for current state and affected historical valuations in `backend/tests/integration/test_reorg.py`.
- [ ] T066 [P] [US3] Add history-range, no-PnL, textual-chart and mobile/keyboard browser tests in `tests/e2e/history.spec.ts`.

### Implementation

- [ ] T067 [US3] Create observation invalidation and indexed history-summary storage without deleting raw observations in `backend/migrations/versions/006_history.py`.
- [ ] T068 [US3] Implement canonicality rechecks, dependent-snapshot invalidation and replacement balance scheduling in `backend/src/audr/jobs/canonicality.py`.
- [ ] T069 [US3] Materialize actual valuation-point summaries with balance/quote provenance and membership/exclusion markers on publication in `backend/src/audr/portfolio/history.py`.
- [ ] T070 [US3] Implement 24h/7d/30d/all history selection with <=2,000 actual chart points, preserved gaps and cursor continuation in `backend/src/audr/portfolio/history_query.py`.
- [ ] T071 [US3] Implement authenticated `GET /history` and paged `GET /history/{snapshot_id}` with exact constituent observation links and no synthetic backfill in `backend/src/audr/api/history.py`.
- [ ] T072 [P] [US3] Build accessible history chart and equivalent point table using recorded timestamps and visible quality/scope markers in `frontend/src/components/HistoryChart.tsx` and `frontend/src/components/HistoryTable.tsx`.
- [ ] T073 [US3] Build range selection, gap and invalidation explanations without PnL terminology in `frontend/src/pages/HistoryPage.tsx`.
- [ ] T074 [US3] Add backend history query/performance fixture generator with 50 wallets, 100 identities, 500 held pairs and 8,760 hourly points in `backend/tests/fixtures/history_scale.py`.
- [ ] T075 [US3] Run history/reorg/browser checks and record their fixture results and limitations in `docs/verification-history.md`.

**Checkpoint**: US3 shows only observed historical facts; earlier snapshots are not silently recalculated.

---

## Phase 6: User Story 4 — Control and recover operation (Priority: P2)

**Goal**: Manage connections, polling, status, export and ordinary restart recovery through the web UI; exceptional host recovery remains documented.

**Independent Test**: Change/pause schedules, coalesce manual work, simulate provider failure, export records and restart API/worker/DB with persistent volumes; compare stored records and run status before/after.

### Required tests — write failing checks first

- [ ] T076 [P] [US4] Add schedule validation, pause/resume, usage projection, cooldown, cancelled work and post-downtime coalescing tests in `backend/tests/integration/test_schedules.py`.
- [ ] T077 [P] [US4] Add authenticated JSON/CSV schema-version, record-type/required-field, exact-value, historical metadata-revision, unknown-versus-zero, coverage, exclusion-revision and full-history reconciliation, formula-neutralization and credential/session redaction tests in `backend/tests/contract/test_exports.py`.
- [ ] T078 [P] [US4] Add persistent-volume restart, migration-failure, wrong/missing key and worker-lease recovery tests in `backend/tests/integration/test_restart.py`.
- [ ] T079 [P] [US4] Add provider-data purge preview/confirmation, job fencing and retained onchain-record tests in `backend/tests/integration/test_provider_purge.py`.
- [ ] T080 [P] [US4] Add settings/status/export/password-change browser journeys at 390px and 1440px, asserting visible last attempt, last success and next execution status, in `tests/e2e/operations.spec.ts`.

### Implementation

- [ ] T081 [US4] Implement revision-checked balance/discovery/quote schedule and freshness/budget validation with usage projections in `backend/src/audr/settings/schedules.py`.
- [ ] T082 [US4] Wire scheduler persistence, pause/resume, next-due calculation, one missed run after downtime and manual-run budget sharing in `backend/src/audr/jobs/scheduler.py`.
- [ ] T083 [US4] Implement authenticated settings, job list/detail/cancel and sanitized operational status routes that expose last attempt, last success and next execution in `backend/src/audr/api/settings.py`, `backend/src/audr/api/jobs.py` and `backend/src/audr/api/status.py`.
- [ ] T084 [US4] Implement private streamed current-portfolio and full-history JSON/CSV exports using the versioned record schema in the HTTP contract, with exact strings, explicit unknown status, coverage, membership/exclusion and source provenance, no chart sampling and no secrets in `backend/src/audr/operations/exports.py` and `backend/src/audr/api/exports.py`.
- [ ] T085 [US4] Implement spreadsheet-formula neutralization for untrusted labels/metadata without changing numeric source fields in `backend/src/audr/operations/csv_safe.py`.
- [ ] T086 [US4] Implement password-protected provider-data purge preview, integration disable/fencing and encrypted monetary payload removal while preserving chain observations in `backend/src/audr/operations/provider_purge.py` and `backend/src/audr/api/data.py`.
- [ ] T087 [US4] Implement exceptional hidden-input host password-reset command that revokes sessions but never replaces encryption keys in `backend/src/audr/operations/reset_password.py`.
- [ ] T088 [US4] Add migration failure/readiness tests and transactional upgrade handling against committed fixture data in `backend/tests/integration/test_migrations.py` and `backend/src/audr/operations/migrate.py`.
- [ ] T089 [P] [US4] Build English schedule/budget settings and explicit cost-versus-freshness warnings in `frontend/src/pages/SchedulesPage.tsx`.
- [ ] T090 [P] [US4] Build job/worker/cooldown/recovery status and cancellation UI showing last attempt, last success and next execution in `frontend/src/pages/StatusPage.tsx`.
- [ ] T091 [US4] Build account/data page for password change, protected exports and purge preview/confirmation in `frontend/src/pages/AccountDataPage.tsx`.
- [ ] T092 [US4] Document Compose startup, loopback setup, TLS proxy/origin, key-loss behavior, restart persistence, exports and reset command in `docs/operations.md`.
- [ ] T093 [US4] Run the US4 fixture/browser tests and reconcile exported records with source rows in `backend/tests/` and `tests/e2e/operations.spec.ts`.

**Checkpoint**: US4 completes operational self-hosting without adding full backup/restore.

---

## Phase 7: Polish & Cross-Cutting Verification

**Purpose**: Verify the complete first release against constitutional gates and measurable outcomes.

- [ ] T094 [P] Add accessibility checks for keyboard, text chart alternatives, English-only content and 390px/1440px layouts across all four journeys in `tests/e2e/accessibility.spec.ts`.
- [ ] T095 [P] Build a reproducible p50/p95 warm dashboard/history benchmark and separate catalog logical-call report for the reference host/fixtures in `scripts/benchmark.py` and `docs/benchmark.md`.
- [ ] T096 [P] Audit exposed routes and logs for keys, wallet-signing capabilities, provider URL leakage, CSRF bypass and unconfigured telemetry in `backend/tests/integration/test_security_boundaries.py`.
- [ ] T097 Add one-command deterministic backend/frontend/e2e/benchmark validation entries from the quickstart contract in `compose.test.yaml` and record actual results in `docs/verification.md`. Run and record a timed owner walkthrough from initial setup through saving two wallets with valid connection details supplied; exclude external scan time and report elapsed time against the five-minute SC-001 limit.
- [ ] T098 Verify all FR-001–FR-024 and SC-001–SC-008 against implemented tests and document known catalog, pricing, RPC and stale-data limits in `docs/release-1-coverage.md`.
- [ ] T099 Document dependency/catalog license attribution and exact pinned versions/digests used by the release image in `docs/third-party.md`.

**Checkpoint**: Release-1 evidence exists for accounting, access, failures, persistence, responsive accessibility and operational limits.

---

## Dependencies & Execution Order

### Phase dependencies

1. Phase 1 → Phase 2: project/build scaffold before migrations, crypto, worker and fixtures.
2. Phase 2 → US1: shared persistence, key lifecycle and fenced jobs precede authenticated scans.
3. US1 → US2: valuation requires exact tracked holdings; US1 itself needs no quote key.
4. US2 → US3: history requires published valuation snapshots; canonicality checks also use US1 balance sets.
5. US1 + US2 + US3 → US4 final acceptance: schedule controls can be developed after Phase 2, but export and complete restart reconciliation require all recorded data types.
6. All desired stories → Polish: benchmark, security and full FR/SC mapping cover the integrated release.

### Within each story

Write its required tests first and capture a failing result. Apply migrations before services; services/providers before routes; routes before connected UI. Do not run two tasks that edit the same file concurrently, even if another part of their story is marked `[P]`. Phase checkpoints are independently testable increments, not permission to skip later release scope.

### Parallel opportunities

| Story | Safe parallel example after prerequisites | Join point |
|-------|-------------------------------------------|------------|
| US1 | T021–T025 tests in separate files; T034 catalog packaging while auth/wallet work proceeds; T043 frontend auth screens alongside RPC adapter | T046 end-to-end holdings journey |
| US2 | T047–T050 fixture/tests; T060 display helpers alongside backend valuation work | T061–T063 connected dashboard |
| US3 | T064–T066 tests; T072 chart/table while history query is built | T073–T075 history journey |
| US4 | T076–T080 tests; T089 and T090 distinct settings/status screens | T091–T093 operational journey |

## Implementation Strategy

1. Deliver Phase 1 + Phase 2 + US1 as the smallest useful MVP: authenticated Ethereum ETH/ERC-20 holdings with honest discovery coverage and no pricing dependency.
2. Add US2 for USD portfolio totals, allocation, exclusions and stale/unknown valuation semantics.
3. Add US3 for immutable observed history, gaps, scope changes and reorg handling.
4. Add US4 for web operation, exports and ordinary restart recovery; then complete cross-cutting release checks.

News, AI recommendations, notifications, activity/security monitoring, additional networks, DeFi/NFT valuation, historical PnL, SaaS and full backup/restore remain outside this task list. No task requires a wallet private key, signature, transaction or proprietary indexer.
