# Gitea CI/CD overlay — versioned source of record

The audr build/test/deploy pipeline runs on **Gitea Actions**, not GitHub Actions.
GitHub `br3d/audr` is the canonical repo; a one-way mirror on the deploy host
replays it into Gitea `dfbot/audr` and, for `main` only, bakes
the three workflow files in this directory into the mirrored commit as
`.gitea/workflows/`. See `docs/deploy-runbook.md`.

The public GitHub-side workflows are a separate, non-deploying thing — the test
gate behind the README badges and a manual container release. They are described
in `docs/github-actions.md`, and the trigger rules there exist precisely to keep
the mirror from replaying them on this runner.

## Why the files are not at `.gitea/workflows/` in this repo

The mirror pushes every branch **except** `main` verbatim. If `.gitea/workflows/`
were tracked upstream, Gitea would find workflows on every long-stale branch the
mirror syncs and start replaying the full suite for each of them. `ci.yaml`
documents this hazard inline (AUD-333). Keeping the files under
`ci/gitea-overlay/workflows/` gets them versioned, reviewable and diffable
without ever becoming a live trigger path.

## Authority and drift

The **running** copies live at `~/.config/audr-mirror/overlay/.gitea/workflows/`
on the deploy host; the mirror script reads that directory, not this one. The
files here are the source of record and the recovery copy — before this directory
existed, `deploy.yaml` (including the whole migration-aware rollback and image
pinning added after the 2026-09-29 outage) existed nowhere but that one host
directory, and the stale copies on the `ci/gitea-actions` branch had drifted by
~190 lines.

Edit here, commit, then push to the host:

```bash
scripts/sync-ci-overlay.sh          # diff repo vs host, then apply on confirmation
scripts/sync-ci-overlay.sh --check  # diff only; exit 1 if they differ
```

Both need `AUDR_DEPLOY_HOST` (environment or `deploy.env` — see
`deploy.env.example`): this repository is public, so no host address is tracked
in it. For the same reason the workflows read the image registry from the Gitea
repository variable `AUDR_REGISTRY` (Settings → Actions → Variables) and refuse
to run if it is unset.

`--check` is the drift guard: run it whenever the pipeline behaves unexpectedly,
since a hand-edit on the host is invisible to code review until the diff is run.

`audr-github-mirror.sh` is a read-only copy of the host's `~/bin/` mirror script,
kept here for the same recovery reason. `scripts/sync-ci-overlay.sh` does not
manage it — update it on the host by hand and re-copy.
