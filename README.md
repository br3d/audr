<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/audr-logo-dark-512.png">
    <source media="(prefers-color-scheme: light)" srcset="assets/brand/audr-logo-light-512.png">
    <img src="assets/brand/audr-logo-light-512.png" alt="audr" width="170">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/br3d/audr/actions/workflows/tests.yml"><img src="https://github.com/br3d/audr/actions/workflows/tests.yml/badge.svg?branch=main" alt="tests"></a>
  <a href="https://github.com/br3d/audr/actions/workflows/release.yml"><img src="https://github.com/br3d/audr/actions/workflows/release.yml/badge.svg" alt="release"></a>
  <a href="https://github.com/br3d/audr/tags"><img src="https://img.shields.io/github/v/tag/br3d/audr?sort=semver&label=version&color=4c1" alt="version"></a>
  <a href="https://github.com/br3d/audr/pkgs/container/audr-backend"><img src="https://img.shields.io/badge/ghcr.io-audr--backend-2496ed?logo=docker&logoColor=white" alt="container image"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/br3d/audr?color=blue" alt="licence"></a>
</p>

# audr

A standalone, self-hosted Ethereum portfolio tracker. You run it on your own
hardware, point it at the public addresses you care about, and it tells you what
they hold and what that is worth in USD — without ever asking for a private key.

Later releases extend the same installation into EVM-focused news, read-only AI
recommendations and address-security monitoring. See the
[roadmap](docs/roadmap.md) for the release boundaries.

> **Status: release 1 is implemented and deployed.** The backend, the SPA, the
> worker, the migrations and the Docker Compose stack all exist and run. What is
> *verified* is tracked honestly in
> [release-1-coverage.md](docs/release-1-coverage.md) — including the gaps.
> `audr` is the repository name; "AUDR Crypto" is an older working title still
> used in some of the planning documents.

---

## What it does

- Tracks **many public Ethereum mainnet addresses** for **one owner**. Adding an
  address asserts nothing about who controls it.
- Reads **native ETH and ERC-20 balances** over ordinary JSON-RPC. No proprietary
  indexer, no portfolio API.
- Discovers tokens from a **vendored token catalog** plus **manual contract
  additions**. Coverage is explicit — the UI never implies that every token you
  hold has been found.
- Values holdings in **USD** from a quote provider, with **exact decimal
  arithmetic**. Binary floating point is never the authoritative representation.
- Records **history from the moment you connect an address onward**. It does not
  backfill, zero-fill, or invent periods before tracking began, and it does not
  claim to compute PnL.
- Distinguishes **unknown from zero** everywhere. A failed read is labelled as a
  failed read; a stale balance is labelled stale and carries its last-success
  time.
- Runs **background jobs** (balance scans, discovery, quotes, event indexing,
  news, icons) on owner-configurable schedules that can be paused, retriggered
  and inspected.

### What it deliberately does not do

It never requests or stores private keys or seed phrases, never signs or submits
transactions, and never approves spending. "Read-only" describes its blockchain
capability — your own settings and records are of course writable. There is no
DeFi position decomposition, no NFT valuation, no historical cost-basis
accounting, and no multi-user mode.

---

## Quick start

Requires Docker Engine 26+ with Compose v2, and `openssl`.

```bash
git clone https://github.com/br3d/audr.git && cd audr

bash scripts/setup-secrets.sh     # generates secrets/ and .env — idempotent
docker compose up -d
curl -s http://localhost/health/live    # {"status":"ok"}
```

Then open <http://localhost/> and create the owner password (12–128 characters).
Everything else — RPC endpoint, quote provider, wallets, schedules — is
configured in the web interface. You should not need to edit a file or open a
shell for routine operation.

**No API keys are required to get started.** audr ships keyless defaults for
every external provider: public JSON-RPC endpoints, the keyless CoinMarketCap
public API for quotes, and Trust Wallet / CoinGecko for token icons.
Configuring your own Infura/Alchemy RPC URL or a CoinGecko Demo key is an
*upgrade*, not a prerequisite.

Optional demo data:

```bash
./scripts/seed_dev.sh            # owner + a demo wallet + the RPC integration
```

Only port **80** is published, and only HTTP is served. For anything reachable
beyond localhost, terminate TLS in a reverse proxy in front of it — see
[operations.md](docs/operations.md#tls--https-proxy). Do not expose an unclaimed
setup screen to the internet.

The full operator guide — secrets, seeding, key-loss recovery, data purge,
reset, troubleshooting — is [docs/operations.md](docs/operations.md).

---

## Architecture at a glance

```
[Browser] ──HTTP :80──► [api / FastAPI :8000]
                           │  SPA bundle (React) + /api/* + /health/*
                           ▼
                  [db / PostgreSQL 16]
                           ▲
           [worker] ──periodic jobs───┘
```

Three long-running containers (`db`, `api`, `worker`) plus one one-shot
bootstrap (`migrate`). Three of the four share a single backend image. Only
`api` publishes a port; everything else talks over a private bridge network.

| Layer | Technology |
|---|---|
| Backend | Python 3.14, FastAPI, SQLAlchemy 2.0 async, psycopg3, Alembic |
| Database | PostgreSQL 16 |
| Frontend | React 19, Vite, TanStack Query, Recharts, hash routing (no router library) |
| Static serving | The `api` process itself — `StaticFiles` with an SPA index fallback |
| Packaging | Docker Compose; multi-stage build with digest-pinned bases |

The API process has no background work in it at all: everything periodic lives
in the separate `worker` process, so a slow RPC scan can never starve HTTP
request handling.

For the component-by-component account — package map, data model, job
scheduling, the auth and encryption model, provider failover — read
[docs/architecture.md](docs/architecture.md). For why there are four compose
services and not one, read [docs/containers.md](docs/containers.md).

---

## Repository layout

```
backend/            FastAPI application, worker, Alembic migrations, pytest suites
  src/audr/         api/ auth/ wallets/ assets/ portfolio/ providers/ settings/
                    jobs/ operations/   (see docs/architecture.md)
  migrations/       Alembic revisions; baseline 0001, head 0016
  tests/            unit/ contract/ integration/ fixtures/
frontend/           React SPA (src/pages, src/components, src/api) + Vitest specs
tests/e2e/          Playwright end-to-end specs (run locally, not in CI)
assets/brand/       Logo masters and the derived transparent PNGs (docs/brand.md)
scripts/            setup-secrets, seed_dev, test, build, deploy, smoke-test,
                    benchmark, registry-prune, gen_third_party, sync-ci-overlay,
                    gen_brand_assets
ci/gitea-overlay/   Versioned source of record for the Gitea Actions workflows
.github/workflows/  Public GitHub Actions: the test gate and the manual
                    container release (docs/github-actions.md)
deploy.env.example  Template for the untracked deploy.env: registry and deploy
                    host for the scripts/ helpers (no addresses are tracked)
compose.yaml        The production/local stack
compose.test.yaml   Ephemeral test stack (db-test, provider-mock, test runners)
Dockerfile          3-stage build: frontend-builder, backend-builder, runtime
                    (runtime carries the SPA bundle at /app/static)
docs/               Everything below
specs/              Spec Kit artefacts for release 1 (spec, plan, contracts)
```

---

## Development

The whole stack runs in containers; there is no host Python or Node requirement
for the normal loop.

```bash
./scripts/test.sh                  # backend pytest + frontend Vitest
./scripts/test.sh --backend-only   # while iterating
./scripts/test.sh --frontend-only
```

For a faster UI loop, run the Vite dev server against a running stack:

```bash
cd frontend && npm install && npm run dev     # http://localhost:5173
```

Branching and merging follow [docs/engineering-workflow.md](docs/engineering-workflow.md):
branch off `main`, run the suite, and **merge your own branch**. This project
does not use pull requests.

Releases are semantic versions cut with `./scripts/release.sh major|minor|patch`;
the running build reports itself at `GET /api/v1/version` and in the bottom-left
of the sidebar. See [docs/releases.md](docs/releases.md).

The GitHub [`tests`](.github/workflows/tests.yml) workflow runs lint and both
suites on every push to `main` and every pull request — that is the first badge
above. Container images are published from GitHub only by the manual
[`release`](.github/workflows/release.yml) workflow, where you pick the version
to build; see [docs/github-actions.md](docs/github-actions.md).

Full developer setup — the e2e suite, lint and type checks, migrations,
debugging against containers, and the shared-checkout worktree rules — is in
[docs/development.md](docs/development.md).

---

## Documentation index

**Start here**

| Document | What it covers |
|---|---|
| [architecture.md](docs/architecture.md) | How the system is built: processes, packages, data model, jobs, auth, providers, configuration reference |
| [api.md](docs/api.md) | The HTTP API as implemented — every route, its auth requirement and its behaviour |
| [development.md](docs/development.md) | Developer setup, every test suite and how to run it, lint/typecheck, migrations, conventions |
| [operations.md](docs/operations.md) | Operator guide: install, secrets, seeding, TLS, key loss, purge, reset, troubleshooting |

**Product and scope**

| Document | What it covers |
|---|---|
| [product-vision.md](docs/product-vision.md) | Purpose, owner experience, boundaries, measures of value |
| [roadmap.md](docs/roadmap.md) | Release 1/2/3 scope and the backlog |
| [discovery.md](docs/discovery.md) | Dated decision record — why the scope is what it is |
| [.specify/memory/constitution.md](.specify/memory/constitution.md) | The five non-negotiable principles the implementation is held to |
| [specs/001-ethereum-portfolio/](specs/001-ethereum-portfolio/) | Release-1 specification, technical plan, research, data model, contracts |

**Infrastructure and operations**

| Document | What it covers |
|---|---|
| [containers.md](docs/containers.md) | Why each compose service exists, what was removed, what could still go |
| [deploy-runbook.md](docs/deploy-runbook.md) | The guarded Gitea deploy pipeline, its invariants, manual recovery, registry retention |
| [github-actions.md](docs/github-actions.md) | The public GitHub workflows: the test gate, the manual image release, GHCR and the README badges |
| [releases.md](docs/releases.md) | Semantic versioning: what each bump means here, cutting a release, image tags, verifying what is deployed |
| [security-at-rest.md](docs/security-at-rest.md) | Encryption-at-rest threat model and recommendation |
| [third-party.md](docs/third-party.md) | Licence attribution and exact release image/dependency pins |
| [brand.md](docs/brand.md) | Where the logo files live, how the derived assets are generated, how to use the mark |

**Verification**

| Document | What it covers |
|---|---|
| [release-1-coverage.md](docs/release-1-coverage.md) | Every FR/SC mapped to the test that proves it — or recorded as a gap |
| [verification.md](docs/verification.md) | One-command test invocations and a recorded pass/fail run log |
| [verification-history.md](docs/verification-history.md) | The history/reorg verification narrative |
| [benchmark.md](docs/benchmark.md) | The warm-read latency gate and the catalog call-count report |

---

## Security notes

Found a vulnerability? Please report it privately — see
[SECURITY.md](SECURITY.md). Do not open a public issue for it.

- Provider credentials (RPC URLs, API keys) are encrypted at rest with
  AES-256-GCM under a master key that is itself wrapped by `SECRET_KEY`. They are
  redacted from logs, from API responses and from exports.
- **`SECRET_KEY` is not recoverable.** Back up `secrets/master_key.hex`
  somewhere safe. Losing it means re-entering every provider credential; see
  [key-loss behavior](docs/operations.md#key-loss-behavior).
- Wallet addresses, balances and history are **not** encrypted at the column
  level. The threat model and the recommended volume encryption are in
  [security-at-rest.md](docs/security-at-rest.md).
- The session cookie is set without the `Secure` flag because the stack serves
  plain HTTP internally; TLS belongs to the proxy in front of it. If you expose
  audr publicly, put it behind HTTPS.
- RPC targets are validated against SSRF: link-local, cloud-metadata and
  multicast destinations are denied and redirects are refused. Private nodes
  require an explicit owner opt-in.
- Self-hosting does not mean nobody observes you — a configured RPC or quote
  provider still sees the requests you send it. Each integration discloses what
  it receives.

---

## Licence

GNU General Public License v3.0 — see [LICENSE](LICENSE). Third-party
attribution and pinned versions are recorded in
[docs/third-party.md](docs/third-party.md).
