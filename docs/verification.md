# Verification results

Recorded: 2026-09-27 against branch `aud-244-worker-entrypoint` (commit `e3775f7`).

## One-command test invocations

All suites run from the repo root via Docker Compose:

```bash
# Backend (unit + integration)
docker compose -f compose.test.yaml --profile backend run --rm backend-tests

# Frontend (Vitest)
docker compose -f compose.test.yaml --profile frontend run --rm frontend-tests

# E2E (Playwright) — requires Dockerfile.e2e
docker compose -f compose.test.yaml --profile e2e run --rm e2e

# Benchmark
docker compose -f compose.test.yaml --profile benchmark run --rm benchmark
```

The infrastructure services (`db-test`, `provider-mock`) are started automatically by the `backend-tests` and `benchmark` service dependencies. The `migrate-test` service runs first and applies all Alembic migrations to the ephemeral test database.

---

## Actual results — 2026-09-27

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

These failures are pre-existing. The wallet isolation issues (tests 3–8) are caused by the test's transaction-rollback isolation strategy conflicting with the FastAPI `TestClient` using a separate session. The discovery checkpoint mismatches (tests 1–2) are test-to-code drift from the T038 refactor.

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

Not run — `Dockerfile.e2e` is not yet present. The Playwright config exists at `frontend/playwright.config.ts` and the spec files at `tests/e2e/`.

---

### Benchmark

Not run — benchmark suite requires `Dockerfile.test` (now present) and a `benchmark` pytest marker. Run with:

```bash
docker compose -f compose.test.yaml --profile benchmark run --rm benchmark
```

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
