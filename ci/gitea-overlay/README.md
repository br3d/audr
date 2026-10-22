# Gitea CI/CD overlay — versioned source of record

The audr build/test/deploy pipeline runs on **Gitea Actions**, not GitHub Actions.
GitHub `br3d/audr` is the canonical repo; a one-way mirror on the deploy host
replays it into Gitea `dfbot/audr` and, for `main` only, bakes the workflow
files in this directory into the mirrored commit as `.gitea/workflows/`, reading
them straight out of that commit. See `docs/deploy-runbook.md`.

There are two of them, and the split is by trigger, not by phase:

| File | Trigger | What it does |
|---|---|---|
| `deploy.yaml` | push to `main`, `v*` tags, `workflow_dispatch` | `lint` + `backend-tests` + `frontend-tests`, then a `build-deploy` job that `needs:` all three. The whole delivery pipeline, test gate included (AUD-687). |
| `build.yaml` | `workflow_dispatch` only | Builds and pushes an image without deploying it. |

`ci.yaml` is gone. It held those three test jobs and fired on the same
`push: main` as `deploy.yaml` with no ordering relation to it, which made the
test suite advisory: the deploy migrated the live database beside it. The jobs
moved verbatim into `deploy.yaml`, where `needs:` is what the suite's verdict
actually means. **A deleted workflow must also be deleted from the host
fallback** (`~/.config/audr-mirror/overlay/.gitea/workflows/`) or the mirror goes
on baking it in — `scripts/sync-ci-overlay.sh` now reports and removes host-only
files for exactly this reason.

The public GitHub-side workflows are a separate, non-deploying thing — the test
gate behind the README badges and a manual container release. They are described
in `docs/github-actions.md`, and the trigger rules there exist precisely to keep
the mirror from replaying them on this runner.

## Why the files are not at `.gitea/workflows/` in this repo

The mirror pushes every branch **except** `main` verbatim. If `.gitea/workflows/`
were tracked upstream, Gitea would find workflows on every long-stale branch the
mirror syncs and start replaying the full suite for each of them. `deploy.yaml`
documents this hazard inline, as do the GitHub-side workflows. Keeping the files under
`ci/gitea-overlay/workflows/` gets them versioned, reviewable and diffable
without ever becoming a live trigger path.

## Authority

**These files are what runs.** `audr-github-mirror.sh` reads the workflow content
out of the commit it is mirroring — this directory in GitHub `main` — and bakes
it in as `.gitea/workflows/`. Edit here, commit, push to `main`, and the next
mirror tick runs the new version. There is no host-side step.

`~/.config/audr-mirror/overlay/.gitea/workflows/` on the deploy host still
contributes any file the commit does **not** carry, so a host-only workflow — or
a commit predating these copies — keeps working. It cannot override a file the
commit provides.

It used to be the other way round: the host directory was authoritative and this
one was a recovery copy kept in agreement by hand. That cost an outage —
**AUD-443**. AUD-439 split the compose files and updated `deploy.yaml` here,
nobody ran the sync script, and the stale host copy went on copying
`compose.yaml` without its new `compose.deploy.yaml` overlay. The stand silently
downgraded to the public image, whose alembic tree predates the live schema, and
stayed down for two hours. Reading from the commit removes that whole drift class
instead of asking someone to remember a step.

```bash
scripts/sync-ci-overlay.sh --check  # diff repo vs host overlay; exit 1 if they differ
scripts/sync-ci-overlay.sh          # push the repo copies to the host on confirmation
```

Those are now a **maintenance tool, not a release step**: useful for pruning a
stale host copy so the fallback cannot surprise you, and for host-only files.
They need `AUDR_DEPLOY_HOST` (environment or `deploy.env` — see
`deploy.env.example`): this repository is public, so no host address is tracked
in it. For the same reason the workflows read the image registry from the Gitea
repository variable `AUDR_REGISTRY` (Settings → Actions → Variables) and refuse
to run if it is unset.

`audr-github-mirror.sh` is a copy of the host's `~/bin/` mirror script, kept here
for recovery. It is the one file still outside the mechanism above — it is what
*performs* the baking, so it cannot bootstrap itself out of the commit it is
reading. `scripts/sync-ci-overlay.sh` does not manage it either: edit it here,
then copy it to `~/bin/audr-github-mirror.sh` on the host by hand.

## "Shipped" means deployed, not mirrored (AUD-706)

The mirror keeps three state files under `~/.config/audr-mirror/`, and the point
of having three is that the first one is not evidence of a deploy:

| file | written by | means |
|---|---|---|
| `last-main-sha` | mirror, at push time | the GitHub `main` it last mirrored |
| `mirrored-gitea-sha` | mirror, at push time | the Gitea `main` commit it pushed for that sha |
| `deployed-gitea-sha` | `deploy.yaml`, on success, `main` only | the Gitea `main` commit that actually deployed |

`deployed == mirrored` is the only state that means main is live. Until AUD-706
the mirror had `last-main-sha` alone and wrote it at push time, so a commit whose
tests went red — `build-deploy` correctly `skipped` by the gate — was recorded as
shipped and never retried. The stand kept serving its previous image: healthy,
`/health/ready` green, and not `main`. AUD-701 is the worked example, and it
needed a human to notice and clear the file by hand.

When the two disagree the mirror re-mirrors the commit to re-run the pipeline,
`AUDR_MIRROR_DEPLOY_RETRIES` times (default 1), and then raises one board alert
under `--key main-undeployed` and goes quiet. The alert closes itself on the
first tick that sees main live again. A retry mints a *new* Gitea commit — the
attempt number is in the commit message precisely so the sha differs, because an
identical commit pushes as a no-op and Gitea would fire no run.

Two deliberate properties:

* It compares **Gitea** shas, not GitHub ones. The deploy job already has that
  value in `GITHUB_SHA`, so the handshake costs it one `printf` and no parsing.
  It also means a `v*` tag or a dispatch build registers as what it is: a
  dispatch deploy of a scratch branch writes a sha matching no mirrored main, so
  it can never read as "main shipped" — which is exactly how AUD-701 ran
  unnoticed, with the stand on a scratch-branch image from run #471.
* A host upgraded from before AUD-706 has no `mirrored-gitea-sha`, so there is
  nothing to compare. The mirror logs that the deploy state is unknown and does
  nothing — no false alert, no pointless rebuild. The next real `main` change
  records it and the mechanism engages from then on.

`backend/tests/unit/test_mirror_deploy_state.py` drives the real script through
all of this against scratch repositories standing in for GitHub and Gitea.
