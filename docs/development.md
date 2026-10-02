# Development guide

How to set up, run, test and change audr. Operators want
[operations.md](operations.md); this is for people editing the code.

Prerequisites: Docker Engine 26+ with Compose v2, `git`, `openssl`. Node 22 and
Python 3.14 are needed **only** for the optional host-side loops below — the
normal workflow runs entirely in containers.

---

## 1. First run

```bash
git clone https://github.com/br3d/audr.git && cd audr
bash scripts/setup-secrets.sh
docker compose up -d
curl -s http://localhost/health/live       # {"status":"ok"}
./scripts/seed_dev.sh                      # optional demo data
```

`docker compose up` builds from source when the registry images are not present.
`migrate` runs `alembic upgrade head` and then initialises the master key, and
`api`/`worker` wait for it to exit 0.

### Demo data

`scripts/seed_dev.sh [BASE_URL]` creates the owner with the canonical test
password `Rand0mP@ssw0rd` (the same one `backend/tests/fixtures/seed.py` uses),
registers the Vitalik Buterin demo wallet, and configures plus validates the RPC
integration. It is unconditional and safe to re-run: the wallet address is
unique-indexed, so a second run gets a 409 and changes nothing.

The RPC URL is resolved from `AUDR_SEED_RPC_URL`, then `secrets/rpc_url.txt`,
then an `AUDR_SEED_RPC_URL=` line in `.env`. Only the hostname is ever printed.
When none is available the step is skipped with a note and the rest still
succeeds — the keyless public endpoints cover the gap. `AUDR_SEED_WALLET=skip`
runs the RPC step only.

---

## 2. Edit loops

### Backend

The backend image bind-mounts nothing in `compose.yaml`, so a code change needs
a rebuild:

```bash
docker compose up -d --build api worker
docker compose logs -f api worker
```

The test image **does** bind-mount `./backend` read-only, which is why
`./scripts/test.sh` picks up edits without rebuilding the source layer.

### Frontend

Containerised rebuilds are slow for UI work. Run Vite directly instead:

```bash
cd frontend && npm install
npm run dev          # http://localhost:5173, HMR
```

`vite.config.ts` proxies `/api` to `http://localhost:8000` — the backend port
**directly**, which the compose stack does not publish. Either publish it
temporarily:

```yaml
# compose.override.yaml — local only, do not commit
services:
  api:
    ports: ["8000:8000"]
```

or point the proxy at nginx on `:80` for the session. Running `npm run dev`
against an unmodified stack will fail every API call, and the symptom (a
perfectly rendered shell with no data) is easy to misread as a backend bug.

### Available npm scripts

`dev`, `build` (`tsc -b && vite build`), `preview`, `test`, `test:watch`,
`test:coverage`, `typecheck`, `e2e`.

---

## 3. Tests

### The one command

```bash
./scripts/test.sh                  # backend pytest + frontend Vitest
./scripts/test.sh --backend-only
./scripts/test.sh --frontend-only
```

Run the full suite before merging. The script uses a **per-invocation compose
project** (`audr-test-$$`, override with `AUDR_TEST_PROJECT`) because
`compose.test.yaml` hardcodes `name: audr-test` and concurrent runs used to
destroy each other's database. It brings up `db-test`, rebuilds `migrate-test`
and `backend-tests`, runs pytest, then runs `npm ci` and Vitest, teeing output
to `.test-backend.log` and `.test-frontend.log`, and tears everything down on
exit.

### Individual suites

```bash
# Backend — unit + non-slow integration + contract
docker compose -f compose.test.yaml --profile backend run --rm backend-tests

# Frontend — Vitest
docker compose -f compose.test.yaml --profile frontend run --rm frontend-tests

# Benchmark — warm-read p95 gate and the catalog call-count report
docker compose -f compose.test.yaml --profile benchmark run --rm benchmark
```

The test stack is fully isolated: `db-test` keeps its data on tmpfs with fixed
throwaway credentials, and `provider-mock` (WireMock) stubs the RPC and quote
boundaries over its admin API. No test ever reaches a real provider or reuses
production credentials. The hardcoded test secrets
(`test_password_only_for_ci`, an all-zero `MASTER_KEY_HEX`) are deliberately
inert.

### Backend test layout

| Directory | Marker | What it needs |
|---|---|---|
| `backend/tests/unit/` | `unit` | Nothing. Pure logic: money math, crypto, config, RPC target validation, policy. |
| `backend/tests/contract/` | `contract` | Provider fixtures. Pins the behaviour of each outbound adapter: RPC reader, failover, rate limiting, `eth_getLogs`, CoinGecko, CoinMarketCap, icons. |
| `backend/tests/integration/` | `integration` | A live PostgreSQL. ~40 files covering the API contract, auth and CSRF boundaries, every endpoint group, job scheduling and leases, valuation and pricing, reorg and restart, migrations, exports and purge. |
| `backend/tests/fixtures/` | — | Shared stubs: `EthRpcStub`, `CoinGeckoStub`, seeded client and wallets, bulk history data. |

`asyncio_mode = "auto"`, so async tests need no decorator. `conftest.py` gives
each test a function-scoped session in a transaction that is rolled back
afterwards, plus a `FakeClock` for time-dependent behaviour. Select a subset the
usual way:

```bash
docker compose -f compose.test.yaml --profile backend run --rm backend-tests \
  pytest -m unit -q
```

### Frontend tests

Vitest with jsdom. Specs live in **two** places — `frontend/src/test/*.test.tsx`
and `frontend/tests/*.test.ts(x)` — which is a historical split, not a
distinction. Check both when adding coverage.

### End-to-end

Playwright, in `tests/e2e/`: `auth`, `rpc`, `valuation`, `history`,
`operations`, `accessibility`.

```bash
cd frontend
npx playwright install chromium          # once
APP_URL=http://localhost OWNER_PASSWORD='Rand0mP@ssw0rd' npm run e2e
```

Two things to know. First, **e2e is not containerised and does not run in CI** —
the `e2e` profile referenced a `Dockerfile.e2e` that never existed and was
removed in AUD-329. Second, the specs run against a **live, seeded stack**, so
bring one up and run `scripts/seed_dev.sh` first; `baseURL` defaults to
`http://localhost:5173` (the Vite dev server), so set `APP_URL` when targeting
the compose stack on `:80`. Runs are serial with no retries.

`docs/release-1-coverage.md` records that there is no green e2e run against
`main`. Treat a failure as possibly pre-existing, and check before assuming your
change caused it.

### Standalone

```bash
python3 scripts/test_registry_prune.py    # no dependencies
```

These cannot live in the pytest suite because the backend test container mounts
only `backend/`.

---

## 4. Lint, types and formatting

Configured in `backend/pyproject.toml`:

- **ruff** — target `py314`, line length 100, rules `E,F,I,UP,B,S,ANN`, with
  per-file ignores for tests and `src/audr/api/**`.
- **mypy** — strict, `python_version 3.14`, pydantic plugin,
  `warn_unreachable`.
- **coverage** — source `src/audr`, omitting tests and migrations.

```bash
cd backend && uv run ruff check . && uv run mypy src
cd frontend && npm run typecheck
```

---

## 5. Database migrations

Alembic, in `backend/migrations/`. The chain is linear from the squashed
baseline `0001` to head `0016`; `file_template` is `%%(rev)s_%%(slug)s`.

```bash
# Create a revision against a running stack
docker compose exec api python -m alembic revision -m "add thing"

# Apply
docker compose run --rm -T migrate
```

Rules that are not negotiable:

- **Every migration must have a working `downgrade`.** The deploy pipeline's
  rollback path downgrades the schema using the *new* image before restoring the
  old one; a migration that cannot be reversed turns a failed deploy into a
  manual recovery. See [deploy-runbook.md](deploy-runbook.md).
- **Never branch the revision chain.** Rebase your revision onto the current
  head instead of merging two heads.
- **Data migrations are separate from schema migrations** where practical —
  `0007`, `0009`, `0010` and `0013` are data-only, which makes them safe to read
  and reason about independently.
- Add new tables to `models.py` only if the ORM needs them; several tables are
  deliberately queried with `sa.text` instead.

---

## 6. Debugging a running stack

```bash
docker compose ps
docker compose logs -f api worker
docker compose exec api curl -s http://localhost:8000/health/ready
docker compose exec db psql -U audr -d audr
docker compose exec db psql -U audr -d audr -tAc \
  'select version_num from alembic_version'
docker compose exec db psql -U audr -d audr -c \
  'select kind, status, error, heartbeat_at from job_run order by claimed_at desc limit 20'
```

Remember that the API is not reachable from the host — only `web:80` is
published, so debug through nginx or `exec` into the container.

---

## 7. Branching, merging and the shared checkout

The full rules are in [engineering-workflow.md](engineering-workflow.md). The
short version:

1. Branch off `main`: `feat/aud-NNN-slug` or `fix/aud-NNN-slug`.
2. Implement, with tests.
3. `./scripts/test.sh` green; if deployed behaviour changed, a green staging
   deploy and `./scripts/smoke-test.sh` too.
4. **Merge your own branch.** This project does not use pull requests.

`/home/codex/git/audr` is a single working tree shared by multiple agents and
runs. Use a **linked worktree** rather than checking a branch out in it:

```bash
git worktree add ../audr-aud-NNN -b feat/aud-NNN-slug main
```

And never move `main` with a plumbing ref update (`git update-ref`,
`git branch -f`) from elsewhere — it changes the pointer without touching the
owning checkout's index, leaving the reverse diff of your merge staged there,
which the next run can commit by accident. Run the merge in the checkout that
owns `main`, or drive it with `git -C /home/codex/git/audr`.

---

## 8. Build and deploy

The authoritative pipeline is **Gitea Actions**, whose versioned source of
record is `ci/gitea-overlay/` (deliberately not tracked at `.gitea/workflows/`,
so the mirror does not replay the suite for every stale branch). `ci.yaml` runs
the backend and frontend suites on push to `main`; `deploy.yaml` is a single
serialized, guarded build-and-deploy job. Read
[deploy-runbook.md](deploy-runbook.md) before touching either, and use
`scripts/sync-ci-overlay.sh --check` as a drift guard.

The shell scripts are the manual fallback:

```bash
./scripts/build.sh [TAG]        # build + push both images
./scripts/deploy.sh [TAG]       # scp compose.yaml, pin tags, migrate, restart, health-gate
./scripts/smoke-test.sh [URL]   # 5 external checks
./scripts/ci.sh                 # build → test → deploy → smoke
```

Deploying to **production** requires founder approval; merging to `main` does
not.

---

## 9. Conventions

- **Decimal everywhere.** `numeric` columns, `Decimal` in Python, `decimal.js`
  in the browser, exact strings on the wire. A float in a financial path is a
  bug.
- **Unknown is not zero.** Model the third state explicitly.
- **Assets are chain + contract**, never symbol.
- **Published rows are immutable.** Correct with new rows plus invalidation
  records.
- **Secrets never leave the envelope** — not in responses, logs, exports or
  error messages. Add a redaction test when you add a credential path.
- **Keyless by default.** A new provider needs a zero-config path before it
  needs a key.
- **Runtime configuration belongs in the database and the UI**, not in
  environment variables. Env vars are for deployment-shaped facts only.
- Commit messages: `type(scope): summary (AUD-NNN)`.
- Comments explain *why*, especially where a line is load-bearing because of a
  past incident. The ones already in `compose.yaml`, `deploy.yaml` and
  `compose.test.yaml` are not noise — each marks something that caused an
  outage. Do not strip them.

---

## 10. Where to look next

| Question | Document |
|---|---|
| How does this system fit together? | [architecture.md](architecture.md) |
| What does the API do? | [api.md](api.md) |
| How do I run and recover a deployment? | [operations.md](operations.md) |
| Why are there this many containers? | [containers.md](containers.md) |
| How does deploy and rollback work? | [deploy-runbook.md](deploy-runbook.md) |
| What is actually tested? | [release-1-coverage.md](release-1-coverage.md) |
| What is in scope at all? | [roadmap.md](roadmap.md), [product-vision.md](product-vision.md) |
