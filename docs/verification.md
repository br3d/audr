# Verification: how to run the suites, and what to trust for "is it passing"

This file is a living pointer, not a scoreboard — the backend/frontend pass
counts below were accurate for one commit on 2026-09-27 and had drifted far
enough from reality by 2026-10 (the suite roughly tripled) that a named test
quoted here as a known failure was no longer informative about whether it
still fails. Treat anything below with a date on it as a point-in-time
record, not the current state.

**For current pass/fail status**, use one of:

- The `tests` badge on [the README](../README.md), or the
  [`tests.yml` run history](https://github.com/br3d/audr/actions/workflows/tests.yml)
  it links to — the public gate, with the two suite logs uploaded as a run
  artifact. See [github-actions.md](github-actions.md).
- The test jobs inside `ci/gitea-overlay/workflows/deploy.yaml` — the
  authoritative pre-deploy gate on the self-hosted runner; a push to `main`
  cannot reach the migration step with a red suite (AUD-687).
- `./scripts/test.sh` locally, which runs the same `compose.test.yaml` stack
  either of the above do.

For dated, narrative records of specific verification drills (migrations,
restore drills, spikes, E2E bug hunts) see
[verification-history.md](verification-history.md), which is append-only and
does not get rewritten the way this file's old pass-count section did.

## One-command test invocations

All suites run from the repo root via Docker Compose:

```bash
# Backend (unit + integration)
docker compose -f compose.test.yaml --profile backend run --rm backend-tests

# Frontend (Vitest)
docker compose -f compose.test.yaml --profile frontend run --rm frontend-tests

# E2E (Playwright) — run locally against a dev stack, there is no container profile
cd frontend && npm run e2e

# Benchmark: warm dashboard/history p50/p95 + catalog logical-call report
docker compose -f compose.test.yaml --profile benchmark run --rm benchmark
```

The infrastructure services (`db-test`, `provider-mock`) are started automatically by the `backend-tests` service dependencies. The `migrate-test` service runs first and applies all Alembic migrations to the ephemeral test database. The `benchmark` service depends on `migrate-test` only — it stubs the provider boundary in-process and makes no outbound HTTP calls, so it does not need `provider-mock`.

> The `e2e` / `api-test` compose profiles were removed in AUD-329 — they referenced a `Dockerfile.e2e` that never existed. The `benchmark` profile was removed in the same change (it invoked `pytest --benchmark-only` against a suite with no `pytest-benchmark` dependency and no `benchmark` marker) and re-added in AUD-108 as a real service that runs `scripts/benchmark.py`. See `docs/benchmark.md`.

**E2E status (AUD-699): local-only, by decision, not by oversight.** The 107
Playwright specs under `tests/e2e` are not run by any CI job and are not
expected to be — containerizing them needs a real `Dockerfile.e2e` and an app
service that actually serves the SPA and accepts the auth settings the specs
use (the blockers AUD-329 hit), and no one has signed up to build that. Until
someone does, this is the gate: `tests/e2e` is typechecked by
`frontend: npm run typecheck` (so it at least compiles), and the suite itself
is **run by hand before every release** — see the pre-release checklist in
[`docs/releases.md`](releases.md#pre-release-checklist) — and whenever the
auth, dashboard, history, or settings pages change. A release cut without a
green `npm run e2e` run noted in its PR/issue is a release cut on an
unverified suite.

---

## Historical snapshot — 2026-09-27

Recorded once, against branch `aud-244-worker-entrypoint` (commit `e3775f7`),
and never re-run since — the counts below are **not** today's suite size. Do
not read the table as "tests expected to fail"; check the current CI run
instead (see above).

### Backend unit tests

```
$ docker run --rm \
    -v "$(pwd)/backend:/workspace/backend:ro" \
    audr-test:local \
    pytest --tb=short -q -m "unit"

......................................
38 passed in 2.12s
```

**38/38 passed.** No dependencies required.

---

### Backend integration tests (non-slow)

```
$ docker compose -f compose.test.yaml run --rm backend-tests

61 passed, 9 failed in 16.26s
```

**61/70 passed.** Known failures:

| Test | Failure | Root cause |
|---|---|---|
| `test_checkpoint_is_saved_and_restored` | `TypeError: unexpected keyword argument 'run_id'` | Test calls `save_discovery_checkpoint(run_id=...)` but function signature uses `wallet_address=` (API updated in T038, test not updated) |
| `test_checkpoint_is_none_for_fresh_run` | `TypeError: unexpected keyword argument 'run_id'` | Same — `get_discovery_checkpoint` signature mismatch |
| `test_add_duplicate_wallet_returns_409` | `assert 201 == 409` | Wallet isolation via transaction rollback does not prevent duplicate-detection query from seeing other session's uncommitted data |
| `test_list_wallets_returns_added_wallets` | wallet list empty | Same isolation issue — wallet created in fixture not visible to list query |
| `test_get_wallet_by_id` | `assert 404 == 200` | Wallet created in fixture not visible in follow-up test |
| `test_patch_wallet_label` | `assert 404 == 200` | Same |
| `test_stop_wallet` | `assert 404 == 200` | Same |
| `test_reactivate_wallet` | `assert 404 == 200` | Same |
| `test_retry_budget_exhaustion_moves_to_failed` | `assert UUID(...) is None` | Claim returns stale record from previous failed test run; DB not clean between runs |

These were the known failures **in this specific 2026-09-27 run**, not a
standing baseline — the suite has grown substantially since and this table
has not been re-verified against it. The wallet isolation issues (tests 3–8)
were caused by the test's transaction-rollback isolation strategy conflicting
with the FastAPI `TestClient` using a separate session. The discovery
checkpoint mismatches (tests 1–2) were test-to-code drift from the T038
refactor. Whether any of this still reproduces is a question for the current
CI run, not this file.

---

### Frontend tests (Vitest)

```
$ docker run --rm -v "$(pwd)/frontend:/workspace" node:22-alpine \
    sh -c "cd /workspace && npm ci && npm test -- --run"

 ✓ tests/client.test.ts         (12 tests)
 ✓ tests/SetupPage.test.tsx     (5 tests)
 ✓ tests/SignInPage.test.tsx    (5 tests)
 ✓ src/test/smoke.test.ts       (1 test)
Test Files  4 passed (4)
Tests       23 passed (23) in 8.78s
```

**23/23 passed.**

---

### E2E tests (Playwright)

Not run in CI — there is no containerised profile. The Playwright config exists at `frontend/playwright.config.ts` and the spec files at `tests/e2e/`; run them locally with `cd frontend && npm run e2e` against a dev stack.

**Run 2026-10-02** against `tests/e2e/operations.spec.ts` (54 cases covering the US4 SchedulesPage, StatusPage, and AccountDataPage journeys at 390px and 1440px viewports), with a local dev stack up (backend on `:8000`, Vite dev server on `:5173`):

```bash
cd frontend
OWNER_PASSWORD=<owner password> APP_URL=http://localhost:5173 npm run e2e -- ../tests/e2e/operations.spec.ts
```

This spec had never been run before. The first runs surfaced real stale-selector bugs, fixed in place rather than removed:

- `toBeFocusable()` was called in ten assertions but was never a registered Playwright matcher (`expect.extend` was never set up for it) — every call threw `TypeError: ... toBeFocusable is not a function`. Replaced with a local `expectFocusable()` helper (`locator.focus()` + `expect(locator).toBeFocused()`).
- `getByLabel('New password')` and `getByLabel('Provider')` each resolved to 2 elements in strict mode — "New password" is a substring of "Confirm new password", and "Provider" is a substring of the purge section's `aria-label="Provider data purge"`. Added `{ exact: true }`.
- The SchedulesPage assertions targeted `<fieldset>` elements; `SchedulesPage.tsx` has never rendered fieldsets — each schedule is a `.schedule-card` div. Updated the locators to match, which also exposed a latent ambiguity in the "cost warning" test's `.or()` locator (it now matched both the warning paragraph and an unrelated static paragraph) — narrowed to the `[role="note"]` element.
- "cost versus freshness" text has never existed in the UI; the real copy is "Free-tier providers typically cap…". Updated the assertion to match it.
- StatusPage never said "worker heartbeat" or "next execution" — the real field labels are "Last Heartbeat" and "Next scheduled runs". Updated both assertions.
- The purge preview button's accessible name is "Preview purge impact" (from its `aria-label`); the regex `/preview impact/i` is not a substring of that name and never matched. Fixed to `/preview purge impact/i`.
- The AccountDataPage three-section check used `getByRole('region', ...).or(getByText(...))`, which is ambiguous because each section's `<h2>` heading duplicates its `aria-label` text (3-way match for "Change password"). Simplified to a single `getByRole('region', ...)` locator per section.

With all of the above fixed, a clean run passed **51/54**. The remaining 3 failures were `signIn()`/navigation timeouts (`waitForSelector`/`toBeVisible` exceeding their 5–30s budgets), all in the first ~90 seconds of the run while this shared host was still busy with concurrent docker/test activity from other agents; every test after that point passed in 7–13s each. That is host contention in this particular shared dev environment, not a selector or product defect — the same three cases had passed on earlier runs when the host was less loaded.

**Run 2026-10-02** against `tests/e2e/accessibility.spec.ts` (20 cases covering keyboard navigation, chart text alternatives, English-only content, and 390px/1440px layouts across all four journeys — auth, valuation, history, operations; AUD-107), with an isolated dev stack (a freshly built `audr-backend` runtime image + ephemeral Postgres, migrated to head and bound to `127.0.0.1:8000`; Vite dev server on `:5173`):

```bash
cd frontend
OWNER_PASSWORD=<owner password> APP_URL=http://localhost:5173 npm run e2e -- ../tests/e2e/accessibility.spec.ts
```

This spec had never been run before either. The first run surfaced 4 real failures, fixed in place:

- `DashboardPage.tsx` renders the page title twice — once in the shared topbar `<h1 class="topbar-title">` (from `Layout.tsx`'s `PAGE_TITLES` map) and again in its own `<h2 class="page-heading">Overview</h2>`. Both share the accessible name "Overview", so `getByRole('heading', { name: /overview/i })` resolved to 2 elements in strict mode (3 call sites: the keyboard-nav test and both viewport variants of the layout test, which share one source line). Disambiguated with the `level: 2` role option to target the page's own heading rather than the topbar.
- The "history range switcher" keyboard test asserted `aria-pressed="true"` on the "1W" button immediately after `focus()`, with no activation step. `DashboardPage.tsx`'s default `historyPeriod` state is `'30d'` ("1M"), not `'7d'` ("1W") — focusing a button doesn't press it, so the assertion was checking a precondition that was never true. Fixed by asserting `aria-pressed="false"` first, then pressing `Enter` (matching the activation pattern the adjacent "history journey: range tabs" test already used) before asserting `"true"`.

With both fixed, two consecutive clean runs passed **20/20**, no flakes.

---

### Benchmark

**Run 2026-10-02 at full reference scale (4,380,000 `valuation_line` rows) — SC-004 met.** Worst measured warm p95 is the dashboard at **149.42 ms** against the 3,000 ms budget (20.1× headroom); the slowest history window (`period=all`) is 130.76 ms. The same run reports **20,402 logical provider calls** for one full catalog-driven scan, a floor of 34 min at the configured 10 calls/s.

The host carried 0.86 load per CPU at start, above the benchmark's 0.7 contention threshold, so those latency figures are recorded as an **upper bound** rather than clean-host numbers — the margin makes the SC-004 verdict safe regardless. See **`docs/benchmark.md`** for the full tables, the reference-host spec, the fixture identity and the methodology. Reproduce with:

```bash
docker compose -f compose.test.yaml --profile benchmark run --rm benchmark
```

The run exits non-zero when a measured p95 breaks the SC-004 3s budget, so it is a gate and not only a report.

---

## Owner walkthrough — SC-001 timing

**SC-001** requires setup from scratch through saving two wallets to complete within **5 minutes** (excluding external scan time).

**Walkthrough procedure** (run on the deployment host):

```
T+00:00  bash scripts/setup-secrets.sh        # generate secrets if missing
T+00:05  docker compose up -d                 # start all services
T+01:20  docker compose ps                    # wait for api to be healthy
T+01:30  open http://localhost/ in browser    # load Setup page
T+01:45  enter password, click Set up         # initialise account
T+02:00  sign in                              # authenticate
T+02:15  click Add wallet                     # open wallet form
T+02:25  enter wallet address #1, save        # first wallet saved
T+02:35  click Add wallet                     # open wallet form again
T+02:45  enter wallet address #2, save        # second wallet saved
T+02:50  DONE
```

**Elapsed (excluding docker image pull):** ~2 min 50 s.

Docker pull (first run) adds ~2–4 minutes on a typical connection; this is excluded from the SC-001 timer. With images pre-pulled (standard for re-deploys), the setup completes well within the 5-minute limit.
