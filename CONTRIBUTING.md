# Contributing to audr

Thanks for looking. audr is a self-hosted Ethereum portfolio tracker, GPLv3, and
the repository is public — so patches from outside the core team are welcome.

This document is the short path from a clone to a change that can be merged. Two
things are worth knowing before you read further:

- **The internal team does not use pull requests.** Engineers merge their own
  green branches into `main` ([docs/engineering-workflow.md](docs/engineering-workflow.md)).
  Outside contributors cannot do that, and are not expected to: for you, a
  **fork and a pull request** is the mechanism, and **infraLead reviews and
  merges it**. See [How to send a change](#how-to-send-a-change) — this
  asymmetry is deliberate, not an oversight.
- **Security issues do not go through here.** Report them privately per
  [SECURITY.md](SECURITY.md). Do not open a public issue or a PR that describes
  a vulnerability.

---

## Is your change in scope?

audr has a narrow, written scope, and the fastest way to waste an afternoon is
to implement something that was deliberately left out. Before building anything
substantial, read [docs/roadmap.md](docs/roadmap.md) and
[docs/product-vision.md](docs/product-vision.md), and skim
[docs/discovery.md](docs/discovery.md) — a dated record of *why* the scope is
what it is.

Things that are out of scope by decision, not by neglect: private keys or
signing of any kind, DeFi position decomposition, NFT valuation, historical
cost-basis / PnL accounting, multi-user mode, and any chain other than Ethereum
mainnet in release 1.

For anything larger than a bug fix or a self-contained improvement, **open an
issue first** and describe what you want to change. A rejected 600-line PR is a
worse outcome for you than a five-line issue comment.

---

## Setting up

Prerequisites: **Docker Engine 26+ with Compose v2**, `git`, `openssl`. The
normal loop runs entirely in containers — you do not need Python or Node on the
host unless you want the fast frontend loop below.

```bash
git clone https://github.com/<you>/audr.git && cd audr
bash scripts/setup-secrets.sh     # generates secrets/ and .env — idempotent
export COMPOSE_FILE=compose.yaml:compose.dev.yaml   # build from your checkout
docker compose up -d --build
curl -s http://localhost/health/live       # {"status":"ok"}
./scripts/seed_dev.sh                      # optional demo data
```

Then open <http://localhost/> and create the owner password. Everything else —
RPC endpoint, quote provider, wallets, schedules — is configured in the web
interface.

**No API keys are needed.** Every external provider has a keyless default path
(public JSON-RPC, the keyless CoinMarketCap public API for quotes, Trust Wallet
/ CoinGecko for icons). If you add a provider, it needs a zero-config path
before it needs a key.

`scripts/seed_dev.sh` creates the owner with the canonical test password
`Rand0mP@ssw0rd`, registers a demo wallet and configures the RPC integration.
It is safe to re-run.

For the editing loops — rebuilding the backend image, running the Vite dev
server with HMR against a live stack, the `compose.override.yaml` port caveat —
read [docs/development.md](docs/development.md). That document is the full
developer guide; this one is only the contribution protocol.

---

## Running the tests

One command runs everything that gates a merge:

```bash
./scripts/test.sh                  # backend pytest + frontend Vitest
./scripts/test.sh --backend-only   # while iterating
./scripts/test.sh --frontend-only
```

It brings up an isolated test stack from `compose.test.yaml` (PostgreSQL on
tmpfs, a WireMock provider stub), runs both suites, tees output to
`.test-backend.log` and `.test-frontend.log`, and tears everything down on exit.
It needs **no secrets** — every credential in that compose file is a deliberately
inert throwaway fixture. No test ever reaches a real provider.

Run the full suite before you ask for a review, not just the half you touched.

The end-to-end Playwright suite in `tests/e2e/` is **not** containerised and does
**not** run in CI. It is not a merge gate, and there is no recorded green e2e run
against `main` — see [docs/release-1-coverage.md](docs/release-1-coverage.md).
Don't block yourself on it, and don't assume an e2e failure is yours.

---

## The mandatory gates

Your change must be green on all four of these. They run on GitHub Actions
([`.github/workflows/tests.yml`](.github/workflows/tests.yml)) for every pull
request, so you will get the same verdict we do — and the self-hosted pipeline
the project actually deploys from is unreachable to you by design.

| Gate | What it is | Run it locally |
|---|---|---|
| **ruff — full rule set** | `ruff check` **and** `ruff format --check`, in `backend/` *and* at the repo root for `scripts/`. Not an "errors only" subset: `E,F,I,UP,B,S,ANN` under `backend/`, the same minus `ANN` at the root. | `cd backend && uv run ruff check . && uv run ruff format --check .` |
| **scripts-parse under 3.13** | Every `scripts/**/*.py` must `compile()` under `python:3.13-slim`. | see below |
| **pytest** | Backend unit + contract + integration suites. | `./scripts/test.sh --backend-only` |
| **Vitest** | Frontend specs. | `./scripts/test.sh --frontend-only` |

```bash
# scripts/ must parse on the oldest python3 we run them under
docker run --rm -v "$PWD:/w" -w /w python:3.13-slim \
  python -c 'import pathlib,sys; [compile(p.read_text(), str(p), "exec") for p in sorted(pathlib.Path("scripts").rglob("*.py"))]'
```

Two gate details that surprise people:

- **ruff is pinned to `0.16.10`**, and its `target-version` is held at **py313**
  while the runtime is Python **3.14**. That is not drift. At `py314` the
  formatter applies PEP 758 and rewrites `except (OSError, ValueError):` into
  `except OSError, ValueError:`, which is a `SyntaxError` on 3.13 — and
  `scripts/*.py` run from a host shell where `python3` is routinely older than
  3.14. The pin matters too: ruff's formatter output moves between patch
  releases. Use the pinned version or your diff will fight CI. The reasoning is
  written up in [docs/development.md](docs/development.md#why-ruff-targets-py313-while-the-runtime-is-314).
- **Every `noqa` must carry a reason.** A bare `# noqa` will be asked about in
  review.

mypy (strict, on `backend/src`) and `npm run typecheck` are not yet CI-gated but
are expected to stay clean:

```bash
cd backend && uv run mypy src
cd frontend && npm run typecheck
```

---

## How to send a change

### If you are an outside contributor

Fork, branch, and open a pull request against `main`. **infraLead reviews and
merges it** — you will not be asked to merge your own work, and you do not need
commit access.

1. Fork the repository and branch off `main`. Any readable branch name is fine;
   `feat/<slug>` or `fix/<slug>` matches what we use.
2. Implement the change **with tests**. A behaviour change without a test is the
   most common reason a PR stalls.
3. Get all four gates green locally (`./scripts/test.sh` plus the lint
   commands). The PR will run them again on GitHub-hosted runners.
4. Open the PR. In the description, say what changed and why, link the issue if
   there is one, and state what you ran. If it touches deployed behaviour or
   migrations, say so explicitly.
5. Expect review comments. This is a small project with no on-call rotation —
   allow about a week for a first response.

Keep one concern per PR. A refactor bundled with a fix takes several times
longer to review than the two of them separately.

### If you are on the core team

You do not open a PR. Branch off `main`, run the suite, and merge your own
branch, per [docs/engineering-workflow.md](docs/engineering-workflow.md) —
including the shared-checkout and linked-worktree rules, which exist because
moving `main` with a plumbing ref update once left the reverse diff of a merge
staged in another run's index. Escalate the merge to infraLead only when it is
genuinely stuck: red tests, an unresolved conflict, a destructive migration,
secret or auth changes, or a decision you do not own.

---

## Commits, versions and tags

**Commit messages:** `type(scope): summary (AUD-NNN)`. The `AUD-NNN` suffix is
an internal issue id — if you don't have one, omit it and reference the GitHub
issue number in the PR description instead.

**Never touch the version.** Versioning is semantic (`MAJOR.MINOR.PATCH`) and
the number lives in four files that must agree: `VERSION`,
`backend/pyproject.toml`, `backend/src/audr/version.py` and
`frontend/package.json`. `./scripts/release.sh major|minor|patch` rewrites all
four together, and `backend/tests/unit/test_version.py` fails the build if they
drift. Hand-editing any one of them turns your PR red. Cutting releases is a
maintainer action.

What the bumps mean here, since this is a self-hosted app with a database
behind it rather than a library: **MAJOR** = the upgrade needs a manual step
(destructive or non-reversible migration, changed/removed config key, dropped
route); **MINOR** = new capability with a migration that applies and downgrades
cleanly; **PATCH** = fixes with no schema or contract impact. The project stays
on `0.x` until a public release is called, so a would-be MAJOR ships as a MINOR
with the manual step in the release notes. Full detail in
[docs/releases.md](docs/releases.md).

**Tags and images.** The annotated tag `vX.Y.Z` is what triggers a deploy on the
self-hosted pipeline. Container images are tagged `<version>-g<sha12>`, with the
bare `<version>` published only for the commit that *is* the release tag.
There is deliberately **no `:latest` on GHCR** — operators pin a version.
GitHub publishes images only through the manual `release` workflow; see
[docs/github-actions.md](docs/github-actions.md).

**Migrations.** Alembic, in `backend/migrations/`, a strictly linear chain.
Every migration **must have a working `downgrade`** — the deploy rollback path
downgrades with the new image before restoring the old one, so an irreversible
migration turns a failed deploy into manual recovery. Never branch the revision
chain; rebase onto the current head.

---

## Conventions review will hold you to

These are the house rules that come up most often. The full list is in
[docs/development.md](docs/development.md#9-conventions).

- **Decimal everywhere.** `numeric` columns, `Decimal` in Python, `decimal.js`
  in the browser, exact strings on the wire. A float in a financial path is a
  bug, not a style preference.
- **Unknown is not zero.** A failed read is labelled a failed read; a stale
  balance is labelled stale and carries its last-success time. Model the third
  state explicitly.
- **Assets are chain + contract**, never symbol.
- **Published rows are immutable.** Correct them with new rows plus invalidation
  records.
- **Secrets never leave the envelope** — not in responses, logs, exports or
  error messages. If you add a credential path, add a redaction test with it.
- **Keyless by default.** A new provider needs a zero-config path before it
  needs a key.
- **Runtime configuration belongs in the database and the UI**, not in
  environment variables. Env vars are for deployment-shaped facts only.
- **Comments explain why.** Several comments in the compose files and the
  workflow files are load-bearing records of past outages. Do not strip them.
- **`compose.yaml` is the install, not the workshop.** It pulls the published
  image and carries three settings a user can act on. Build stanzas go in
  `compose.dev.yaml`, our registry in `compose.deploy.yaml`, and a rationale
  longer than two lines goes in `docs/` or next to the code it explains — not
  into the file a new user reads first.

---

## Licence

audr is **GPLv3** ([LICENSE](LICENSE)). By contributing you agree your
contribution is licensed under the same terms. Third-party attribution and
pinned versions are recorded in [docs/third-party.md](docs/third-party.md); if
you add a dependency, update it.
