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

It moves one more file in the same commit, a carrier of a different kind —
nothing validates it against `VERSION`:

| File | Why it moves |
|---|---|
| `frontend/package-lock.json` | Mirrors `package.json`'s version in two places; `npm ci` aborts if they disagree, and the Dockerfile's frontend stage runs `npm ci`. |

### The one file `release.sh` does *not* touch

`compose.yaml`'s `x-backend-image` names the published image literally, so a
checkout of `v1.4.2` pulls `audr-backend:1.4.2` and an operator can read the tag
without resolving a variable (AUD-439). That makes it look like a sixth version
carrier, and `release.sh` used to rewrite it with the rest — but it is a
different kind of statement. Every file above is a claim about *this
repository* and is true the instant the commit is made. The anchor is a claim
about an *external registry*, and at cut time it is false: the image does not
exist until somebody dispatches `release.yml`.

So the anchor moves in a separate, later commit, written by
[`scripts/promote-image.py`](#publishing-is-three-steps-and-main-works-after-each)
only once it has proven the image is there. The consequence worth stating
plainly: **`main` is installable at every commit.** A clone taken between the
cut and the publish gets the previous release, not a `manifest unknown`
failure (AUD-546).

The same applies to the two healthchecks `compose.yaml` currently inlines.
`api`'s is a copy of `audr.operations.healthcheck` and `worker`'s is a copy of
its `--worker` mode, inlined because the pinned tag predates them and calling
the module would report a clean clone as `unhealthy` (AUD-442, AUD-539). Those
reverts are also claims about what is in the published image, so they happen in
`promote-image.py` too, each gated on its own check of the release commit's
contents. `backend/tests/unit/test_healthcheck.py` fails the build if
`compose.yaml` and `promote-image.py` stop agreeing on the comment the rewrite
anchors on — otherwise the revert would quietly become a no-op and the inline
copies would outlive their reason.

## Cutting a release

```bash
./scripts/release.sh patch          # or minor / major / --set 1.4.2
./scripts/release.sh --show         # print the current version
```

It refuses to run on a dirty tree or off `main`, rewrites the five files above,
commits `Release vX.Y.Z` and creates the annotated tag. Nothing is pushed until
you add `--push`, because **pushing the tag is what deploys**:
`ci/gitea-overlay/workflows/deploy.yaml` triggers on `v*`.

Undo before pushing: `git tag -d vX.Y.Z && git reset --hard HEAD~1`.

### Publishing is three steps, and `main` works after each

```bash
./scripts/release.sh patch --push        # 1. cut:     tag + version files
#    GitHub -> Actions -> release        # 2. publish: the image (manual)
./scripts/promote-image.py --push        # 3. promote: point compose.yaml at it
```

**Step 1 — cut.** Pushes the version bump and the `vX.Y.Z` tag, which is what
triggers our own deploy (`ci/gitea-overlay/workflows/deploy.yaml`). `main` now
says `VERSION=X.Y.Z` while `compose.yaml` still pins the previous release.

**Step 2 — publish.** [Actions → release → Run
workflow](https://github.com/br3d/audr/actions/workflows/release.yml), ref
`vX.Y.Z`. This is what puts `ghcr.io/br3d/audr-backend:X.Y.Z` in the registry.
It is a human action in a browser and cannot be automated from here:
`release.yml` is `workflow_dispatch`-only because the GitHub → Gitea mirror
replays tags verbatim, so a `tags:` trigger would run this workflow on our
self-hosted runner alongside Gitea's own deploy.

Note what it publishes: the bare `:X.Y.Z` tag **only** when the commit it builds
*is* the `vX.Y.Z` tag. Any other ref gets `X.Y.Z-g<sha12>`, which a clone of
`main` will not pull.

**Step 3 — promote.** `./scripts/promote-image.py --push` is the only thing that
moves the `compose.yaml` pin. Before touching the file it checks, against the
real registry:

- `ghcr.io/br3d/audr-backend:X.Y.Z` has a manifest — and asks with the full
  `Accept` header, because GHCR answers `404` for tags that exist when asked
  without it;
- that image's `org.opencontainers.image.revision` label is the commit `vX.Y.Z`
  points at, so a tag built from some other ref is refused by name rather than
  silently promoted;
- whether that commit carries `audr.operations.healthcheck`, its `--worker`
  flag, and the beacon writer in `audr.jobs.__main__` — each inline healthcheck
  is swapped for the module form only if its own answer is yes (AUD-544).

Then, once it knows a rewrite is actually due, one question about the repository
rather than the image: `vX.Y.Z` must be an ancestor of `origin/main` and no more
than 20 commits behind it. Because step 2 is a human click, a tag can sit cut
but unpublished for days — and promoting it afterwards is what decides the
backend every `git clone && docker compose up -d` installs from then on. v0.1.2
spent a week in that state and ended up 106 commits back; publishing and
promoting it would have passed every check above and still pinned fresh clones
to a backend missing the AUD-597 and AUD-595 fixes. The guard's advice in that
case is to cut a fresh release from `main` instead of promoting the overtaken
one; `--allow-stale` overrides it when pinning the older release is deliberate.

Run it before step 2 and it refuses, printing the dispatch URL. Run it twice and
the second run finds nothing to do — the staleness check deliberately sits after
that exit, so an already-promoted release does not start failing its own
`--check` as `main` moves on. `python3 scripts/test_promote_image.py` covers the
rewrite offline; `backend/tests/unit/test_promote_image_drift.py` covers the
staleness guard in CI.

#### Why it is split this way

Steps 1 and 2 cannot be made atomic — step 2 needs a human — so the question is
only what `main` says in between. It used to say "pull `X.Y.Z`" before that tag
existed, which meant a fresh clone failed `docker compose up -d` with `manifest
unknown`; `v0.1.2` was cut 2026-10-05 and sat unpublished for nine days exactly
that way (AUD-442), so the two-command quickstart was dead for nine days. The
window was not self-healing and the instruction that was supposed to prevent it
— "do not start unless you can finish both steps in one sitting" — is not a
mechanism.

Moving the pin bump out of the release commit removes the window instead of
asking anyone to close it, which is the CEO ruling on AUD-545. The invariant it
buys, asserted offline by
`backend/tests/unit/test_version.py::test_compose_image_tag_matches`: **the pin
may trail `VERSION`, never lead it.** An install running one release behind is
strictly better than one that cannot start.

If step 3 is skipped or forgotten, nothing breaks — new users get the previous
release until someone runs it. That is a visible-but-harmless state, unlike the
one it replaces.

Our own deploy host is unaffected by all three: `compose.deploy.yaml` overrides
the `x-backend-image` anchor with
`${AUDR_REGISTRY}/audr-backend:${BACKEND_TAG}`, so it never reads GHCR.

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

GHCR carries **no `:latest`**. A dispatch-only workflow can only move that tag
when somebody remembers to run it, so publicly it would mean "the last build
anyone asked for", not "the newest release"; public operators pin `<version>`
instead. `:latest` in the table above is the private registry's, moved by the
health-gated deploy job for the deploy host's benefit.

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
