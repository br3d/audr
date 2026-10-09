# Third-party attribution and release image pins

This document records the licences of everything audr redistributes, plus the
exact versions and image pins that go into the release images.

- **audr's own licence:** GNU General Public License v3.0 (see `LICENSE`).
- **Reflects commit:** `f480223e4a1f585afa3cb4bc05f13091f435825a` (2026-10-02).
- **Sources of truth:** `Dockerfile` (base image tags), `backend/uv.lock`
  (backend pins), `frontend/package-lock.json` (frontend pins),
  `backend/src/audr/assets/data/` (bundled catalog data).

Regenerate the dependency tables after any lockfile change:

```bash
cd frontend && npm ci && cd ..
backend/.venv/bin/python scripts/gen_third_party.py
```

The script prints the three dependency tables below; the surrounding prose
carries judgement calls (copyleft notes, unpinned apt packages) and is
maintained by hand.

---

## 1. Release image pins

### 1.1 Base images

`Dockerfile` references its bases by tag (`node:22-alpine`, `python:3.14-slim`)
so that a reader can see which runtimes the image is built on, rather than by
`@sha256:` digest. Tags float: the digests below are the
ones those tags resolved to on 2026-10-04, recorded here so a past build can be
reproduced with `docker build --build-context` or by temporarily re-pinning the
`FROM` lines to a digest. Re-check them after any base-image bump with:

```bash
docker buildx imagetools inspect node:22-alpine --format '{{.Manifest.Digest}}'
docker buildx imagetools inspect python:3.14-slim --format '{{.Manifest.Digest}}'
```

| Stage | Image | Digest (as of 2026-10-04) | Upstream licensing |
| --- | --- | --- | --- |
| `frontend-builder` | `node:22-alpine` | `sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402` | Node.js: MIT. Alpine base: MIT/BSD-style packages plus musl libc (MIT) and BusyBox (GPL-2.0-only). Build stage only — not shipped. |
| `backend-builder`, `runtime` | `python:3.14-slim` | `sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d` | CPython: PSF-2.0. Debian `slim` base: mixed DFSG-free licences (GPL-2.0, GPL-3.0, LGPL, MIT, BSD) — per-package texts under `/usr/share/doc/*/copyright` in the image. |

These two stages are all there is. nginx is not redistributed in any form — the
API serves the SPA itself — so no web-server licence applies to this image.

### 1.2 Published images

`scripts/build.sh` and the Gitea `build`/`deploy` workflows tag each build with
the 12-character git SHA of the source commit and also move `latest`. They push
to the registry named by `$AUDR_REGISTRY` (`deploy.env` locally, a Gitea
repository variable in CI); this repository is public and records no endpoint.

| Repository | Tag | Manifest digest (as of 2026-10-02) |
| --- | --- | --- |
| `$AUDR_REGISTRY/audr-backend` | `latest` | `sha256:9e9f2dfe31ffb030f1dca8eb6721832f8912e33407e1a1631495146a880f5c0e` |

There is one image. An `audr-frontend` repository existed earlier and is no
longer built or pushed; whatever tags remain in the registry are orphans.

This digest moves on every build. To resolve the digest for the image a
host is actually running, ask the registry for the manifest of the deployed
tag — the git SHA tag, not `latest`:

```bash
curl -sI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "http://$AUDR_REGISTRY/v2/audr-backend/manifests/<git-sha-tag>" \
  | grep -i docker-content-digest
```

### 1.3 Not pinned

Nothing, in the sense of unattributed licences. The base images are referenced
by tag rather than digest (see 1.1), so two builds of the same commit can sit on
different base revisions; the pinned Python and npm lockfiles, and everything
audr itself ships, do not move.

The `runtime` stage installs no OS packages at all, which is what keeps that
true. In particular it ships no `curl`: the container health check in
`compose.yaml` calls the interpreter already present in the image
(stdlib `urllib`, via `audr.operations.healthcheck` or the equivalent inlined in
`compose.yaml` — see [containers.md](containers.md)). An apt layer would
resolve to whatever the Debian archive served at build time, and pinning an
exact Debian version means pinning the whole apt snapshot. Since `curl` is
neither installed nor redistributed, its
[licence](https://curl.se/docs/copyright.html) (an MIT/X derivative) does not
apply to this image.

The host-side scripts (`scripts/deploy.sh`, `scripts/smoke-test.sh`,
`scripts/seed_dev.sh`) and the Gitea deploy health-gate still use `curl`, but
they run on the deploy host against its own system packages — they are not part
of anything we build or redistribute. Keep container health checks
stdlib-only; adding an apt layer back to `runtime` reintroduces the unpinned
dependency.

---

## 2. Bundled catalog data

Catalog data is **not code** and carries its own terms. Both files are vendored
snapshots committed into the repository, so a fresh install needs no network
call at worker startup (the rationale is in
`backend/src/audr/assets/catalog.py`).

| File | Entries | SHA-256 | Upstream | Licence |
| --- | --- | --- | --- | --- |
| `backend/src/audr/assets/data/uniswap_mainnet_tokenlist.json` | 407 tokens | `3deceee47e6220081d46d379be280f15d09486ae5e6c3c462e7d6412d21341ec` | [Uniswap/default-token-list](https://github.com/Uniswap/default-token-list), `src/tokens/mainnet.json` | GPL-3.0, as declared by the upstream repository |
| `backend/src/audr/assets/data/cmc_map_seed.json` | CMC id map seed | `ab7d5a98349ab9695b5953fc748ca4c1dd405e3cdec0f8655be2da0830993829` | CoinMarketCap keyless `/v1/cryptocurrency/map` response, fetched 2026-10-01 | Factual identifier data redistributed under CoinMarketCap's API terms of use; no open-source licence is granted by CMC |

Notes:

- The Uniswap list is GPL-3.0, which is compatible with audr's own GPL-3.0
  licensing. Older design notes refer to `ethereum-lists/tokens` (MIT); that
  source is **not** what ships, and the table above describes what is actually
  in the image.
- Token names, symbols and contract addresses are third-party trademarks and
  factual data respectively; listing a token implies no endorsement.
- `cmc_map_seed.json` is a seed only. `sync_cmc_map_live` can refresh it from
  CoinMarketCap at runtime; data obtained that way is likewise governed by
  CoinMarketCap's terms, not by this repository's licence.

### Live price and chain data providers

audr does not redistribute provider responses, but it does query them at
runtime. Their data is supplied under their own terms of use, not under any
licence granted by this project: CoinGecko (demo/keyed API), CoinMarketCap
(keyless public and keyed pro API), and whichever Ethereum JSON-RPC endpoint
the operator configures.

---

## 3. Backend runtime dependencies

These are the packages installed into the release image. The image builds with
`uv sync --frozen --no-dev --no-install-project`, so this is the runtime
closure of `backend/pyproject.toml` (including the `uvicorn[standard]` and
`psycopg[binary]` extras) as locked in `backend/uv.lock` — dev and test
dependencies are not shipped.

| Package | Version | License | Depth |
| --- | --- | --- | --- |
| `alembic` | 1.20.0 | MIT | direct |
| `annotated-doc` | 0.0.5 | MIT | transitive |
| `annotated-types` | 0.8.0 | MIT | transitive |
| `anyio` | 4.15.1 | MIT | transitive |
| `argon2-cffi` | 25.1.0 | MIT | direct |
| `argon2-cffi-bindings` | 26.1.0 | MIT | transitive |
| `certifi` | 2026.7.22 | MPL-2.0 | transitive |
| `cffi` | 2.1.1 | MIT-0 | transitive |
| `click` | 8.5.0 | BSD-3-Clause | transitive |
| `cryptography` | 50.0.1 | Apache-2.0 OR BSD-3-Clause | direct |
| `cytoolz` | 1.1.0 | BSD-3-Clause | transitive |
| `eth-abi` | 6.0.0 | MIT | direct |
| `eth-hash` | 0.8.0 | MIT | transitive |
| `eth-typing` | 6.0.0 | MIT | transitive |
| `eth-utils` | 6.0.0 | MIT | direct |
| `fastapi` | 0.141.1 | MIT | direct |
| `greenlet` | 3.5.6 | MIT AND PSF-2.0 | transitive |
| `h11` | 0.16.0 | MIT | transitive |
| `httpcore` | 1.0.9 | BSD-3-Clause | transitive |
| `httptools` | 0.8.0 | MIT | transitive |
| `httpx` | 0.28.1 | BSD-3-Clause | direct |
| `idna` | 3.20 | BSD-3-Clause | transitive |
| `mako` | 1.4.3 | MIT | transitive |
| `markupsafe` | 3.0.3 | BSD-3-Clause | transitive |
| `parsimonious` | 0.10.0 | MIT | transitive |
| `psycopg` | 3.3.6 | LGPL-3.0-only | direct |
| `psycopg-binary` | 3.3.6 | LGPL-3.0-only | transitive |
| `pycparser` | 3.0 | BSD-3-Clause | transitive |
| `pydantic` | 2.13.5 | MIT | direct |
| `pydantic-core` | 2.46.5 | MIT | transitive |
| `pydantic-settings` | 2.15.0 | MIT | direct |
| `python-dotenv` | 1.2.3 | BSD-3-Clause | transitive |
| `pyyaml` | 6.0.3 | MIT | transitive |
| `regex` | 2026.9.10 | Apache-2.0 AND CNRI-Python | transitive |
| `sqlalchemy` | 2.0.54 | MIT | direct |
| `starlette` | 1.7.0 | BSD-3-Clause | transitive |
| `toolz` | 1.1.0 | BSD-3-Clause | transitive |
| `typing-extensions` | 4.16.0 | PSF-2.0 | transitive |
| `typing-inspection` | 0.4.4 | MIT | transitive |
| `tzdata` | 2026.4 | Apache-2.0 (bundles the IANA time zone database, public domain) | transitive |
| `uvicorn` | 0.54.0 | BSD-3-Clause | direct |
| `uvloop` | 0.22.1 | MIT OR Apache-2.0 | transitive |
| `watchfiles` | 1.3.0 | MIT | transitive |
| `websockets` | 17.1 | BSD-3-Clause | transitive |

Notes:

- **`psycopg` / `psycopg-binary` are LGPL-3.0-only** — the only copyleft code
  in the backend runtime set. audr itself is GPL-3.0, so the combination is
  compatible, and the libraries are used unmodified as a dynamically imported
  dependency. Anyone redistributing the image must keep these packages
  replaceable and make their source available on request.
- **`certifi` is MPL-2.0** (file-level copyleft) and is likewise shipped
  unmodified.
- `regex` carries `Apache-2.0 AND CNRI-Python`; it enters the image only as a
  transitive dependency of `parsimonious`, which `eth-abi` requires.

### Backend development dependencies (not shipped)

Declared under `[project.optional-dependencies] dev` and used by
`Dockerfile.test` and CI only.

| Package | Version | License |
| --- | --- | --- |
| `pytest` | 9.1.1 | MIT |
| `pytest-asyncio` | 1.4.0 | Apache-2.0 |
| `pytest-cov` | 7.1.0 | MIT |
| `respx` | 0.23.1 | BSD-3-Clause |
| `anyio[trio]` | 4.15.1 | MIT (`trio`: MIT OR Apache-2.0) |
| `mypy` | 1.17.1 | MIT |
| `types-psycopg2` | 2.9.21.20260911 | Apache-2.0 |
| `ruff` (configured in `pyproject.toml`) | 0.11.13 | MIT |

---

## 4. Frontend runtime dependencies

The `dependencies` closure from `frontend/package-lock.json` — the packages
whose code can end up in the built SPA bundle that is copied into the `runtime`
stage.

| Package | Version | License | Depth |
| --- | --- | --- | --- |
| `@reduxjs/toolkit` | 2.12.0 | MIT | transitive |
| `@standard-schema/spec` | 1.1.0 | MIT | transitive |
| `@standard-schema/utils` | 0.3.0 | MIT | transitive |
| `@tanstack/query-core` | 5.104.0 | MIT | transitive |
| `@tanstack/react-query` | 5.104.0 | MIT | direct |
| `@types/d3-array` | 3.2.2 | MIT | transitive |
| `@types/d3-color` | 3.1.3 | MIT | transitive |
| `@types/d3-ease` | 3.0.2 | MIT | transitive |
| `@types/d3-interpolate` | 3.0.4 | MIT | transitive |
| `@types/d3-path` | 3.1.1 | MIT | transitive |
| `@types/d3-scale` | 4.0.9 | MIT | transitive |
| `@types/d3-shape` | 3.2.0 | MIT | transitive |
| `@types/d3-time` | 3.0.4 | MIT | transitive |
| `@types/d3-timer` | 3.0.2 | MIT | transitive |
| `@types/react` | 19.3.0 | MIT | transitive |
| `@types/use-sync-external-store` | 0.0.6 | MIT | transitive |
| `clsx` | 2.1.1 | MIT | transitive |
| `csstype` | 3.2.3 | MIT | transitive |
| `d3-array` | 3.2.4 | ISC | transitive |
| `d3-color` | 3.1.0 | ISC | transitive |
| `d3-ease` | 3.0.1 | BSD-3-Clause | transitive |
| `d3-format` | 3.1.2 | ISC | transitive |
| `d3-interpolate` | 3.0.1 | ISC | transitive |
| `d3-path` | 3.1.0 | ISC | transitive |
| `d3-scale` | 4.0.2 | ISC | transitive |
| `d3-shape` | 3.2.0 | ISC | transitive |
| `d3-time` | 3.1.0 | ISC | transitive |
| `d3-time-format` | 4.1.0 | ISC | transitive |
| `d3-timer` | 3.0.1 | ISC | transitive |
| `decimal.js` | 10.6.0 | MIT | direct |
| `decimal.js-light` | 2.5.1 | MIT | transitive |
| `es-toolkit` | 1.52.0 | MIT | transitive |
| `eventemitter3` | 5.0.4 | MIT | transitive |
| `immer` | 11.1.18 | MIT | transitive |
| `internmap` | 2.0.3 | ISC | transitive |
| `react` | 19.3.0 | MIT | direct |
| `react-dom` | 19.3.0 | MIT | direct |
| `react-is` | 19.3.0 | MIT | transitive |
| `react-redux` | 9.3.0 | MIT | transitive |
| `recharts` | 3.10.1 | MIT | direct |
| `redux` | 5.0.1 | MIT | transitive |
| `redux-thunk` | 3.1.0 | MIT | transitive |
| `reselect` | 5.2.0 | MIT | transitive |
| `scheduler` | 0.28.0 | MIT | transitive |
| `tiny-invariant` | 1.3.3 | MIT | transitive |
| `use-sync-external-store` | 1.7.0 | MIT | transitive |
| `victory-vendor` | 37.3.6 | MIT AND ISC | transitive |

All frontend runtime licences are permissive (MIT / ISC / BSD-3-Clause); none
are copyleft. `victory-vendor` re-publishes parts of the d3 ecosystem, hence
the combined `MIT AND ISC`.

---

## 5. Frontend build-time dependencies (not shipped)

Used in the `frontend-builder` stage and in CI. Their output — the compiled
bundle — is shipped; the tools themselves are not.

| Package | Version | License |
| --- | --- | --- |
| `@playwright/test` | 1.63.0 | Apache-2.0 |
| `@types/node` | 26.6.3 | MIT |
| `@types/react` | 19.3.0 | MIT |
| `@types/react-dom` | 19.3.0 | MIT |
| `@vitejs/plugin-react` | 6.1.1 | MIT |
| `@vitest/coverage-v8` | 5.0.2 | MIT |
| `jsdom` | 30.1.1 | MIT |
| `typescript` | 7.0.2 | Apache-2.0 |
| `vite` | 8.3.1 | MIT |
| `vitest` | 5.0.2 | MIT |

---

## 6. Obligations summary for redistributors

If you redistribute an audr release image:

1. Ship the GPL-3.0 text (`LICENSE`) and offer the corresponding source.
2. Keep the LGPL-3.0 notice and source offer for `psycopg` / `psycopg-binary`,
   and do not prevent the recipient from replacing those libraries.
3. Keep the MPL-2.0 notice for `certifi`.
4. Preserve the permissive copyright notices for the MIT / ISC / BSD / Apache
   packages listed above; the base images carry their own per-package copyright
   files under `/usr/share/doc`.
5. The bundled Uniswap token list is GPL-3.0 data — attribute
   `Uniswap/default-token-list`.
6. `cmc_map_seed.json` is redistributed under CoinMarketCap's API terms; check
   those terms before shipping it onward.
