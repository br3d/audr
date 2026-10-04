# Releases and versioning (AUD-407)

audr ships as **one image** — API, worker and SPA — so it has **one version
number**, not one per component. That number is a [semantic
version](https://semver.org/): `MAJOR.MINOR.PATCH`.

## Where we are in the numbering

audr stays on **0.x until the founder calls a public release**. Patch and minor
bumps are the only ones in play until then; `1.0.0` is cut on the founder's
word, not automatically when the next breaking change lands. The reason is that
while the only operator is us, a MAJOR bump carries no information nobody in
the room already has — the number starts earning its keep once there are
operators upgrading installs they did not build.

Practical consequence: a change that would be MAJOR by the table below is
released as a MINOR while we are on 0.x, with the manual step written in the
release notes.

## What the parts mean here

This is a self-hosted application with a database behind it, so the usual
library reading of semver is adapted to what actually costs an operator time:

| Bump | When |
|---|---|
| **MAJOR** | An upgrade needs a manual step: a destructive or non-reversible migration, a changed/removed config key, a dropped API route the SPA or an external caller depends on. |
| **MINOR** | New capability, new routes, new settings — with a migration that applies and downgrades cleanly. The upgrade is `deploy and done`. |
| **PATCH** | Fixes and internal changes with no schema or contract impact. |

The rule of thumb: **if an operator must read the release notes before
deploying, it is at least a MINOR, and if they must *do* something, it is a
MAJOR.**

## Where the number lives

One source of truth, three mirrors. None of them can read from another at
runtime — the runtime image contains neither the repo root nor an installed
`audr` distribution — so they are kept in step by tooling instead:

| File | Why it needs its own copy |
|---|---|
| `VERSION` | Source of truth for shell scripts and CI. |
| `backend/pyproject.toml` | Project metadata for the build. |
| `backend/src/audr/version.py` | What `GET /api/v1/version` reports; the only copy inside the runtime image. |
| `frontend/package.json` | What Vite bakes into the SPA bundle, and so what the sidebar shows. |

`scripts/release.sh` rewrites all four together. `backend/tests/unit/test_version.py`
fails the build if they ever disagree, so drift cannot reach `main`.

## Cutting a release

```bash
./scripts/release.sh patch          # or minor / major / --set 1.4.2
./scripts/release.sh --show         # print the current version
```

It refuses to run on a dirty tree or off `main`, rewrites the four files,
commits `Release vX.Y.Z` and creates the annotated tag. Nothing is pushed until
you add `--push`, because **pushing the tag is what deploys**:
`ci/gitea-overlay/workflows/deploy.yaml` triggers on `v*`.

Undo before pushing: `git tag -d vX.Y.Z && git reset --hard HEAD~1`.

## Image tags in the registry

| Trigger | Tag pushed |
|---|---|
| push to `main` | `<version>-g<sha12>` and `latest` |
| release tag `v1.4.2` | `1.4.2` (plus `latest`) |
| `workflow_dispatch` | the tag you typed, else `<version>-g<sha12>` |

Two rules make these tags mean something:

- **The sha stays in the tag between releases.** Every commit on `main` carries
  the same `VERSION`, so a bare version tag would be reassigned on every push
  and `:1.4.2` would silently mean "whatever built last".
- **The bare `<version>` tag is only published after the health-gate passes.**
  A version tag that appears before the gate names an image nobody has proven
  can serve traffic. The deploy job pushes it in step 7, next to `:latest`.

A `v*` ref whose name disagrees with `VERSION` fails the job outright rather
than deploying — see the `Compute tags` step.

### The public mirror of those tags on GHCR

The private registry above is what the deploy host pulls from. For operators who
are not us, the same image is published to `ghcr.io/<owner>/audr-backend` by the
manual GitHub `release` workflow, using the same tag scheme and the same "bare
`<version>` only from the `v<version>` commit" rule. It is dispatch-only and it
deploys nothing — see [github-actions.md](github-actions.md).

## Verifying what is deployed

```bash
curl -s http://<host>/api/v1/version
# {"version":"1.4.2","commit":"<full sha>","built_at":"2026-10-03T12:00:00Z"}
```

`commit` and `built_at` come from Docker build args and are empty in a dev
checkout; `version` is compiled into the source and is always present. The same
values are on the image as OCI labels
(`org.opencontainers.image.version` / `.revision` / `.created`), so
`docker inspect` answers the question without a running container.

The deploy health-gate makes this a hard requirement: it will not call a deploy
successful until `/api/v1/version` reports the commit the job just built. That
is a stronger check than the image-tag assertion next to it — a registry tag can
be moved, a sha baked into the image at build time cannot.

`./scripts/smoke-test.sh` asserts the live version equals the `VERSION` of the
checkout it runs from. When smoke-testing a deliberate rollback, pin the
expected number instead:

```bash
AUDR_EXPECT_VERSION=1.4.1 ./scripts/smoke-test.sh https://<host>
```

## Where a user sees it

Bottom-left of the sidebar, under the "Self-hosted" line: `v1.4.2`. Hovering
shows the short commit when the build has one.

It is a compile-time constant, not an API call, so it renders before login,
offline, and during an API outage — the situations where "which build am I
looking at?" is actually being asked.
