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
export COMPOSE_FILE=compose.yaml:compose.dev.yaml   # build from this checkout
docker compose up -d --build
curl -s http://localhost/health/live       # {"status":"ok"}
./scripts/seed_dev.sh                      # dev fixtures — creates the owner with
                                           # the canonical *test* password
```

`compose.yaml` on its own pulls the published release — that is what an operator
installing audr gets. `compose.dev.yaml` is the development overlay that adds
the `build:` stanzas, so the three backend services run the image built from
your working tree (tagged `audr-backend:dev`) instead. Export `COMPOSE_FILE`
once per shell as above and every later `docker compose` in that shell merges
both files; otherwise pass `-f compose.yaml -f compose.dev.yaml` each time.

`migrate` runs `alembic upgrade head` and then initialises the master key, and
`api`/`worker` wait for it to exit 0.

### Demo data

`scripts/seed_dev.sh` is a **development tool, not an end-user feature** — it is
deliberately absent from the README's install path, because it creates the owner
account with a password published in this repository. Never run it against an
instance holding real addresses.

`scripts/seed_dev.sh [BASE_URL]` creates the owner with the canonical test
password `Rand0mP@ssw0rd` (the same one `backend/tests/fixtures/seed.py` uses),
registers the Vitalik Buterin demo wallet, and configures plus validates the RPC
integration. It is unconditional and safe to re-run: the wallet address is
unique-indexed, so a second run gets a 409 and changes nothing.

#### The seed RPC URL

A dev RPC URL usually embeds a provider API key, so it must never reach git.
`seed_dev.sh` resolves it at run time from the first source that is set:

1. the `AUDR_SEED_RPC_URL` environment variable,
2. `secrets/rpc_url.txt` (git-ignored, mode `600`),
3. an `AUDR_SEED_RPC_URL=...` line in `.env` (git-ignored).

```bash
# one-off, nothing stored on disk
AUDR_SEED_RPC_URL='https://mainnet.example/v3/<key>' ./scripts/seed_dev.sh

# or store it once for repeated seeding
install -m 600 /dev/null secrets/rpc_url.txt
printf '%s' 'https://mainnet.example/v3/<key>' > secrets/rpc_url.txt
```

Only the hostname is ever printed. When no URL is available the step is skipped
with a note and the rest still succeeds — the keyless public endpoints cover the
gap. `AUDR_SEED_WALLET=skip` runs the RPC step only.

This is a developer concern only. `scripts/setup-secrets.sh` — the install step
an end user runs — deliberately knows nothing about it: a self-hosting user
configures their RPC endpoint on the Connections page, where the keyless
default already works.

### Repository layout

```
backend/            FastAPI application, worker, Alembic migrations, pytest suites
  src/audr/         api/ auth/ wallets/ assets/ portfolio/ providers/ settings/
                    jobs/ operations/   (see architecture.md)
  migrations/       Alembic revisions; baseline 0001, head 0016
  tests/            unit/ contract/ integration/ fixtures/
frontend/           React SPA (src/pages, src/components, src/api) + Vitest specs
tests/e2e/          Playwright end-to-end specs (run locally, not in CI)
assets/brand/       Logo masters and the derived transparent PNGs (brand.md)
assets/screenshots/ Interface captures used by the README
scripts/            Operator helpers (setup-secrets, backup, restore, seed_dev)
                    plus the build/release/maintenance set — scripts/README.md
                    says which is which
ci/gitea-overlay/   Versioned source of record for the Gitea Actions workflows
.github/workflows/  Public GitHub Actions: the test gate and the manual
                    container release (github-actions.md)
deploy.env.example  Template for the untracked deploy.env: registry and deploy
                    host for the scripts/ helpers (no addresses are tracked)
compose.yaml        The install: published image, no build, five lines of
                    configuration. What an operator runs.
compose.dev.yaml    Development overlay — adds `build:` so the stack runs your
                    working tree
compose.deploy.yaml Private-registry overlay for our own deploy host
                    (AUDR_REGISTRY + BACKEND_TAG); not part of an install
compose.test.yaml   Ephemeral test stack (db-test, provider-mock, test runners)
Dockerfile          3-stage build: frontend-builder, backend-builder, runtime
                    (runtime carries the SPA bundle at /app/static)
docs/               Everything in the index at README.md
```

The technology choices behind each of those directories, and the reasoning for
the process split, are in [architecture.md](architecture.md); the stack is
Python 3.14 / FastAPI / SQLAlchemy 2.0 async on PostgreSQL 16, with a React 19 +
Vite SPA served by the `api` process itself.

---

## 2. Edit loops

### Backend

The backend image bind-mounts nothing, so a code change needs a rebuild — with
the dev overlay active (`COMPOSE_FILE` exported as in §1, or `-f` twice):

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

`vite.config.ts` proxies `/api` to `http://localhost:8000` — the container port
**directly**, which the compose stack does not publish (it publishes `80:8000`).
Either publish it temporarily:

```yaml
# compose.override.yaml — local only, do not commit
services:
  api:
    ports: ["8000:8000"]
```

Note that compose *appends* to `ports` rather than replacing, so that override
publishes both `:80` and `:8000`. If something already holds port 80 on your
machine the stack will refuse to start; use `ports: !override ["8000:8000"]` to
replace the list instead.

Alternatively, point the proxy at the stack's published `:80` for the session —
the API and the SPA are the same origin there. Running `npm run dev` against an
unmodified stack will fail every API call, and the symptom (a perfectly
rendered shell with no data) is easy to misread as a backend bug.

### Available npm scripts

`dev`, `build` (`tsc -b && vite build`), `preview`, `test`, `test:watch`,
`test:coverage`, `typecheck`, `lint`, `format`, `format:check`, `e2e`.

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

Teardown runs from an `EXIT` trap, which a `SIGKILL` skips — so a run killed
mid-suite leaks its whole stack: a running postgres and WireMock, two ~550MB
images and a `node_modules` volume. Six of those took the shared agent host to
89% disk, so every run reaps the previous casualties at startup.

A leftover is recognised by its **`com.docker.compose.service` label** landing in
`compose.test.yaml`'s service set — `db-test`, `provider-mock`, `migrate-test`,
`backend-tests`, `benchmark`, `frontend-tests` — not by the shape of its project
name. Those names never collide with the dev stack (`web`, `init`, `api`,
`worker`, `db`) or the deploy host, so this is narrower than a name prefix while
catching every project name. Compose labels the images and volumes it builds the
same way, so both of those sweeps key off the same set; the image sweep
additionally requires the repository be exactly `<project>-<service>`, because a
*pulled* image keeps the labels of whatever service last used it and a
registry-namespaced tag must never be a reap candidate.

Three guards keep a live stack safe: the current `$PROJECT` is always exempt; a
project is reaped only once its newest container is older than six hours
(`AUDR_TEST_REAP_AGE_H`) and no `*-tests-*` container of it is running, so a
concurrent suite is never torn down; and a stack whose age cannot be established
is left alone rather than removed on an unknown age.

Pinned `AUDR_TEST_PROJECT` stacks are reaped on the same terms as auto-named
ones. Exempting them (the original behaviour) meant nothing *ever* reclaimed
one — not its own run, since `teardown` deliberately drops `--rmi local` when the
project is pinned, and not any later run — so each ad-hoc pinned name cost about
1GB permanently. If you keep a pinned stack to reuse on a cycle longer than six
hours, raise `AUDR_TEST_REAP_AGE_H` past your reuse interval; the only cost of a
reap is the rebuild, served from the BuildKit cache.

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

Vitest with jsdom. Specs live in one place — `frontend/src/test/*.test.ts(x)`,
the only root `vitest.config.ts` includes. Mock factories for `api/client`
should reuse the shared `src/test/mockApiError.ts` class, so `instanceof
ApiError` checks inside components still match the mocked module's export.

### End-to-end

Playwright, in `tests/e2e/`: `auth`, `rpc`, `valuation`, `history`,
`operations`, `accessibility`.

```bash
cd frontend
npx playwright install chromium          # once
APP_URL=http://localhost OWNER_PASSWORD='Rand0mP@ssw0rd' npm run e2e
```

Three things to know. First, **e2e is not containerised and does not run in CI,
deliberately** — AUD-699 settled that: the suite is local-only on a pre-release
cadence (a checklist item in [`releases.md`](releases.md)), because the specs
are order-dependent and host-sensitive in ways a required check cannot absorb.
The reasoning in full, and what would have to change to revisit it, is in
[`verification.md` § E2E tests](verification.md). The specs *are* typechecked on
every CI run, via `tests/e2e/tsconfig.json` which `npm run typecheck` includes —
so a stale import or a renamed export reds the gate even though no browser
starts. Keep them compiling.

Second, the specs run against a **live, seeded stack**, so
bring one up and run `scripts/seed_dev.sh` first; `baseURL` defaults to
`http://localhost:5173` (the Vite dev server), so set `APP_URL` when targeting
the compose stack on `:80`. Runs are serial with no retries.

Third, cases within a file **share state on purpose** — `auth.spec.ts`'s "setup
page is unavailable once an owner exists" depends on an earlier case having
created the owner — so do not reorder them, and do not run one case from
mid-file against a fresh database and expect it to pass.

`docs/release-1-coverage.md` records that there is no green e2e run against
`main`. Treat a failure as possibly pre-existing, and check before assuming your
change caused it.

### Standalone

Nothing is standalone-only any more. The five dependency-free test files that
used to live in `scripts/` moved to `backend/tests/unit/` in AUD-698, because
living there meant living outside every job: `backend/pyproject.toml` pins
`testpaths = ["tests"]`, so nothing collected them and they ran only when
somebody remembered to. They are part of the backend suite now.

They are still runnable on their own, which is useful on a host with no Docker:

```bash
python3 backend/tests/unit/test_registry_prune.py   # no dependencies
python3 backend/tests/unit/test_promote_image.py    # no dependencies, no network
```

Each needs its subject script bind-mounted when it runs in the container; see
`compose.test.yaml`, which carries one mount per script with the reasoning.

---

## 4. Lint, types and formatting

Configured in `backend/pyproject.toml` for `backend/`, and in the repo-root
`ruff.toml` for everything outside it (notably `scripts/`):

- **ruff** — pinned to `0.16.10` in the `dev` extra, target `py313`, line length
  100. Rules `E,F,I,UP,B,S,ANN` under `backend/`, the same minus `ANN` at the
  root, with per-file ignores for tests and `src/audr/api/**`.
- **mypy** — strict, `python_version 3.14`, pydantic plugin,
  `warn_unreachable`. CI-gated (AUD-690) against the frozen backlog in
  `backend/mypy-baseline.txt`, not the raw `mypy` exit code — see
  CONTRIBUTING.md's gates table for the regenerate command.
- **coverage** — source `src/audr`, omitting tests and migrations. `pytest`
  runs with `--cov=audr --cov-report=term-missing` and Vitest with
  `--coverage` (`npm run test:coverage`) on every CI invocation (AUD-690), so
  both numbers are visible in every run's log. Neither is gated on a
  threshold yet — the decision was visibility first, a floor later once there
  is a baseline number to set one against.

```bash
cd backend && uv run ruff check . && uv run mypy src
cd frontend && npm run typecheck
```

### Why ruff targets py313 while the runtime is 3.14

The runtime is Python 3.14 everywhere (`requires-python = ">=3.14"`, every image
is `python:3.14-slim`), but ruff's target version is held one release back on
purpose. At `py314` the formatter applies PEP 758 and rewrites
`except (OSError, ValueError):` into `except OSError, ValueError:`. That form
runs fine on 3.14, and fails with
`SyntaxError: multiple exception types must be parenthesized` on 3.13 and
earlier — which matters because `scripts/*.py` are `#!/usr/bin/env python3` and
are run from a host shell, where `python3` is routinely older than 3.14.
Targeting `py313` keeps one syntax that is valid under both. CI enforces the
floor by byte-compiling `scripts/` under `python:3.13-slim`. See AUD-392.

The pin matters for the same reason: ruff's formatter output moves between
patch releases, so an unpinned ruff makes "is this formatted?" a question about
the day you asked. Keep the version in `backend/pyproject.toml` and the version
in the CI `lint` job in sync.

### What CI actually gates

The `lint` job in `ci/gitea-overlay/workflows/deploy.yaml` runs the full rule set
(`ruff check`) and `ruff format --check`, both in `backend/` and at the repo
root (`scripts/`), plus the `scripts/` 3.13 compile check. Every `noqa` in the
tree carries a reason — see AUD-393.

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

Rules that are not negotiable — and, since AUD-698, enforced rather than
remembered. Both were house rules that nothing checked, which is why the two
guards below exist:

- **Every migration must have a working `downgrade`.** The deploy pipeline's
  rollback path downgrades the schema using the *new* image before restoring the
  old one; a migration that cannot be reversed turns a failed deploy into a
  manual recovery. Worse than that, actually: when the downgrade *fails* the
  workflow has nothing left to try, so it says so and leaves the stack on the
  new build — the one outcome the rollback existed to prevent. See
  [deploy-runbook.md](deploy-runbook.md).

  `scripts/migration-smoke.sh` now runs `upgrade` → `downgrade -3` → `upgrade`
  against the ephemeral test database, through the same `python -m alembic`
  entrypoint the deploy uses, as the `migration_smoke` phase of
  `./scripts/test.sh` and as a step in the Gitea `backend-tests` job. Raise
  `AUDR_MIGRATION_SMOKE_DEPTH` if your branch adds more than three revisions.

  If a change genuinely cannot be reversed — a destructive data repair cannot —
  say so deliberately: add the revision to `IRREVERSIBLE` in
  `backend/tests/unit/test_migration_chain.py` with the reason. An empty
  `downgrade()` that is *not* on that list fails the suite, because an empty
  downgrade is invisible in review (it is the absence of code) and its cost
  lands on whoever is watching a failed deploy decline to roll back.
- **Never branch the revision chain.** Rebase your revision onto the current
  head instead of merging two heads. `test_migration_chain.py` asserts a single
  head and a linear chain: two heads make `alembic upgrade head` refuse to run
  at all, so the next deploy cannot migrate — and the usual way to get there is
  merging two branches that each added a migration, where both diffs look fine
  and it is the *pair* that is wrong. Do not fix it with `alembic merge`; the
  rollback walks the chain with `downgrade -1`, which has no defined meaning at
  a merge point.
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
docker compose exec api python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health/ready').read().decode())"
docker compose exec db psql -U audr -d audr
docker compose exec db psql -U audr -d audr -tAc \
  'select version_num from alembic_version'
docker compose exec db psql -U audr -d audr -c \
  'select kind, status, error, heartbeat_at from job_run order by claimed_at desc limit 20'
```

Remember that the container's `:8000` is not reachable from the host — the
stack publishes `api` on `:80`, so debug through that or `exec` into the
container. The backend image ships no `curl` (AUD-379 dropped the apt layer so
the release image is fully digest-pinned), which is why the HTTP probe above
goes through the interpreter; from the host, `curl http://localhost/health/ready`
still works.

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

The authoritative pipeline is **Gitea Actions**, and the files that run are
`ci/gitea-overlay/workflows/` in this repo — the mirror reads them out of the
`main` commit and bakes them in as `.gitea/workflows/` (they are deliberately not
tracked there, so the mirror does not replay the suite for every stale branch).
Editing one and pushing to `main` is all that is needed. `deploy.yaml` runs the
lint, backend and frontend jobs on push to `main` and then a single serialized,
guarded `build-deploy` job that `needs:` all three — a red suite means no
deploy, not a race with one (AUD-687). Read
[deploy-runbook.md](deploy-runbook.md) before touching either.

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
