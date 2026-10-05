# Gitea CI/CD overlay — versioned source of record

The audr build/test/deploy pipeline runs on **Gitea Actions**, not GitHub Actions.
GitHub `br3d/audr` is the canonical repo; a one-way mirror on the deploy host
replays it into Gitea `dfbot/audr` and, for `main` only, bakes the three workflow
files in this directory into the mirrored commit as `.gitea/workflows/`, reading
them straight out of that commit. See `docs/deploy-runbook.md`.

The public GitHub-side workflows are a separate, non-deploying thing — the test
gate behind the README badges and a manual container release. They are described
in `docs/github-actions.md`, and the trigger rules there exist precisely to keep
the mirror from replaying them on this runner.

## Why the files are not at `.gitea/workflows/` in this repo

The mirror pushes every branch **except** `main` verbatim. If `.gitea/workflows/`
were tracked upstream, Gitea would find workflows on every long-stale branch the
mirror syncs and start replaying the full suite for each of them. `ci.yaml`
documents this hazard inline. Keeping the files under
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
