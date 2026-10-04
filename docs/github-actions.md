# GitHub Actions: the public test gate and the manual image release (AUD-409)

audr has **two** CI systems, and they are not redundant:

| | Where it runs | What it is for |
|---|---|---|
| **Gitea Actions** (`ci/gitea-overlay/workflows/`) | Self-hosted runner on the deploy host | The real delivery pipeline: build, guarded deploy, rollback, registry retention. Private, reachable only from our network. |
| **GitHub Actions** (`.github/workflows/`) | GitHub-hosted runners | The public face: a test verdict anyone can see and reproduce, CodeQL security analysis, badges on the README, and on-demand release images. Deploys nothing. |

The Gitea pipeline stays the authority over what is deployed. Nothing in
`.github/workflows` touches the deployed instance or the private registry.

## `tests.yml` — the gate behind the badge

Runs on **pushes to `main`**, on **pull requests**, and on demand from
`release.yml` via `workflow_call`. Three jobs, each mirroring its counterpart in
`ci/gitea-overlay/workflows/ci.yaml`:

- `lint` — the full ruff rule set plus `format --check`, over `backend/` and
  `scripts/`. Pinned to `ghcr.io/astral-sh/ruff:0.16.10`; that pin must move
  together with the Gitea job and `backend/pyproject.toml`'s dev extra (AUD-392).
- `scripts-parse` — compiles every `scripts/*.py` under Python 3.13, because
  those files are run by the host shell's `python3`, not inside the 3.14 image.
- `tests` — `./scripts/test.sh`, i.e. the same docker-compose stack (postgres +
  WireMock + pytest + Vitest) the local loop uses. It needs **no secrets**: every
  credential in `compose.test.yaml` is a throwaway fixture. The two suite logs are
  uploaded as a run artifact (7-day retention) so a red run can be read without
  re-running it.

### Why `push` is filtered to `main`

The mirror on the deploy host replays this repository into Gitea. Every branch
**except `main`** is pushed verbatim, and Gitea falls back to `.github/workflows`
on a ref that carries no `.gitea/workflows` of its own. An unfiltered `on: push`
here would therefore make the mirror replay the full suite for every stale
branch it syncs, on one self-hosted runner — the hazard AUD-333 is about.
`branches: [main]` closes it: the mirror pushes branch `X` as `refs/heads/X`,
which never matches, and Gitea's own `main` carries the overlay so the fallback
does not apply there either.

**Never add a `tags:` trigger to a file in `.github/workflows`.** Tags are
mirrored verbatim, and a tagged commit has no `.gitea/workflows`, so Gitea would
run the GitHub workflow through the fallback path alongside its own
tag-triggered deploy.

## `release.yml` — manual image builds

`workflow_dispatch` only. Actions minutes and GHCR storage are both metered on
the free plan, and we already get an image per green `main` from Gitea, so
GitHub builds an image when someone asks for one and not otherwise.

Run it from **Actions → release → Run workflow**. The ref selector at the top of
that dialog is how you "choose the version": pick the tag (`v0.4.1`), branch or
use the `ref` input for a sha.

| Input | Default | Effect |
|---|---|---|
| `ref` | *(the selected ref)* | Tag, branch or sha to build. Overrides the selector; use it for a bare commit sha. |
| `run_tests` | ✅ | Runs `tests.yml` against the resolved commit and blocks the build if it fails. Uncheck only to re-publish a ref that already went green. |
| `push_image` | ✅ | Uncheck for a build-only dry run — validates the Dockerfile and consumes no registry storage. |
| `create_github_release` | ✅ | Creates the GitHub Release for `v<version>` when the built commit *is* that tag. On any other commit the job is skipped, not failed. |

### Tags it publishes

Same scheme as `scripts/build.sh` and the Gitea pipeline, so a tag means the
same thing in either registry (see [releases.md](releases.md)):

- `<version>-g<sha12>` — always. Immutable and unambiguous: between releases
  every commit on `main` is still "the same version" by the `VERSION` file, so
  the sha is what keeps two builds of it apart.
- `<version>` — **only** when the commit being built is exactly the `v<version>`
  tag. Publishing it from an arbitrary commit would make `:0.4.1` mean
  "whatever was built last", which is the one guarantee semver tagging buys.
- **no `latest`.** GHCR gets no floating tag at all (decided on AUD-409). It
  could only float when someone happened to dispatch a build, so a public
  `:latest` would name "the last version anyone bothered to publish" rather
  than the newest release — and an operator cannot tell from the tag which
  image they are about to run. Pin `<version>`, or `<version>-g<sha12>` for a
  build between releases. The private registry's own `:latest` is unaffected:
  it exists for the deploy host and is moved by the health-gated Gitea job
  ([releases.md](releases.md)), never by a manual dispatch.

Images land at `ghcr.io/<owner>/audr-backend`, built for `linux/amd64` only —
what the deploy host runs. Provenance and SBOM attestations are disabled: they
publish extra manifests and blobs per build, and the digest is already in the
run summary.

### GHCR housekeeping

- **Make the package public** after the first publish. A new GHCR package is
  private even in a public repository, and private package storage is what
  counts against the free-plan quota; public packages are free to store and to
  pull. Package page → *Package settings* → *Change visibility*.
- **Link it to the repository** on the same page, so the package inherits the
  repo's README and licence.
- **Prune occasionally.** Every dispatch adds a `<version>-g<sha>` tag.
  Deleting old versions is manual on purpose — an automatic retention job here
  could delete an image an operator is still running. The private registry's own
  retention is a separate, automated thing (`scripts/registry-prune.py`,
  AUD-345) and does not touch GHCR.
- Layer caching uses the **Actions** cache (10 GB account-wide), not GHCR, and
  is scoped per version so one release cannot evict another's entry.

## `codeql.yml` — static security analysis

GitHub's own CodeQL analysis over both halves of the tree (AUD-411): a `python`
job covering `backend/` and `scripts/`, and a `javascript-typescript` job
covering the SPA. Free for a public repository, which this one now is — code
scanning is unavailable on a private repository without GitHub Advanced
Security, so making the repository private again would turn this workflow red.

It is **advisory**. Findings land in *Security → Code scanning* and annotate the
commit that introduced them; nothing merges or deploys on CodeQL's verdict, and
`tests.yml` remains the gate a contributor needs to pass.

| | |
|---|---|
| Triggers | push to `main`, `schedule` (Mondays 05:17 UTC), `workflow_dispatch` |
| Query suite | `security-extended` — not `security-and-quality`, whose maintainability queries duplicate ruff and drown the security findings |
| Build | none. Both languages are interpreted, so CodeQL extracts from source; there is no `autobuild` step to misfire on a project whose real build is the Dockerfile |
| Excluded paths | `frontend/dist`, `frontend/node_modules`, `frontend/test-results`, `backend/migrations/versions` |

Weekly rather than nightly because what CodeQL finds between pushes is *new
queries over old code*, and the query bundle moves on a release cadence — a
nightly run would spend metered Actions minutes re-deriving yesterday's answer.
The same `branches: [main]` filter and the same no-`tags:` rule as `tests.yml`
apply, and for the same mirror reason.

Permissions are `security-events: write` (the SARIF upload — the only write it
performs), plus `contents: read` and `actions: read`.

## Secrets

None of the three workflows needs a repository secret. `release.yml` authenticates to GHCR
with the run's built-in `GITHUB_TOKEN` and the narrowest permissions that work:
`contents: read` for the whole file, `packages: write` only on the build job,
`contents: write` only on the release job. No deploy host address, SSH key or
registry endpoint appears anywhere in `.github/` — those live in `deploy.env`
and in Gitea repository variables, because this repository is public.
