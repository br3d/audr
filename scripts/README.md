# Scripts

Seventeen files live here, but an operator only ever runs four of them. The
rest are the team's build, release and maintenance machinery — they assume a
registry, a deploy host or an SSH key that a self-hosting user does not have.

## If you are running audr

| Script | What it does |
|---|---|
| `setup-secrets.sh` | One-time credential initialisation for a fresh install: generates `.env` (`DB_PASSWORD`, `SECRET_KEY`) and keeps a recovery copy of the key in `secrets/master_key.hex`. Idempotent — safe to re-run, never overwrites |
| `backup.sh` | Encrypted Postgres backup. Dumps the `db` service and pipes it straight through encryption, so the dump never touches disk in the clear. Writes a timestamped archive to `./backups` (or `$AUDR_BACKUP_DIR` / `$1`) |
| `restore.sh` | Restores an archive produced by `backup.sh`. Restores **data only** — see the header comment about `SECRET_KEY` / `secrets/master_key.hex`. Needs `--force` to overwrite a database that already has tables |
| `seed_dev.sh` | Fills a running instance with the canonical demo fixtures: owner account, a sample wallet, and the RPC integration when an RPC URL is available. Useful for trying audr out before connecting your own wallets |

Backup, restore and first-run setup are all documented in context in
[`docs/operations.md`](../docs/operations.md); prefer that over reading the
scripts.

## Internal — build, release, deploy

These require `deploy.env` (registry and deploy-host addresses, never tracked)
or an SSH key, and they act on infrastructure outside your installation.

| Script | What it does |
|---|---|
| `test.sh` | The test gate: backend pytest and frontend Vitest in Docker Compose. `--backend-only` / `--frontend-only` |
| `build.sh` | Builds the application image and pushes it to the private registry, tagged `<version>-g<short sha>` |
| `deploy.sh` | Deploys a tag to the remote host over SSH: pulls the image, runs migrations, restarts services |
| `smoke-test.sh` | Post-deploy health probe against a live instance; non-zero exit on any failed check |
| `ci.sh` | The whole pipeline end to end — build → test → deploy → smoke-test — with per-stage skip flags |
| `release.sh` | Cuts a semantic-version release: rewrites the version in the five in-repo files that carry it, commits, and creates the annotated tag. Pushing is opt-in (`--push`) because a pushed `v*` tag triggers the deploy pipeline. Deliberately does **not** touch `compose.yaml` — see `promote-image.py` |
| `promote-image.py` | Step 3 of a release: points `compose.yaml`'s `x-backend-image` at `<version>` *after* proving that tag is in GHCR and was built from the release commit, and restores the inlined healthchecks the same way. Refuses, naming the dispatch, when the publish has not run; idempotent when it has (AUD-546) |
| `sync-ci-overlay.sh` | Reconciles `ci/gitea-overlay/workflows/` with the deploy host's mirror overlay. Maintenance only: the mirror reads the workflows out of the `main` commit, so this is not a step in shipping a workflow change |

## Internal — maintenance and generated files

| Script | What it does |
|---|---|
| `host-gc.sh` | Bounds the deploy host's Docker disk usage. Every push to `main` builds a new image on the host runner and nothing else removes them. Retention is age-based *and* capped by count, and escalates to shorter windows on its own while the disk is above the warn threshold |
| `registry-prune.py` | Enforces a tag-retention policy on the image registry over the `/v2` HTTP API, since the registry has no built-in retention and we have no shell on it |
| `test_registry_prune.py` | Dependency-free unit tests for the pruner: `python3 scripts/test_registry_prune.py`. Not in the pytest suite, which runs in a container that cannot see `scripts/` |
| `dump-prune.py` | Applies the authorised GFS retention (3 days of hourlies + newest dump per older ISO week) to the Paperclip DB dump directory on ai-stuff. Dry-run by default; the durable fix is a board setting an agent token cannot write, so this is the stopgap that has to repeat weekly — see AUD-435 and the script header |
| `test_promote_image.py` | Dependency-free, offline unit tests for the `compose.yaml` rewrite in `promote-image.py`: `python3 scripts/test_promote_image.py` |
| `test_dump_prune.py` | Dependency-free unit tests for the dump pruner: `python3 scripts/test_dump_prune.py`. One case reproduces the 2026-10-11 hand-prune file-for-file |
| `benchmark.py` | Two reports: warm dashboard/history read latency against the reference scale fixture, and the catalog's logical RPC call count. Results are published in [`docs/benchmark.md`](../docs/benchmark.md) |
| `gen_third_party.py` | Regenerates the dependency licence tables in [`docs/third-party.md`](../docs/third-party.md) from the locked runtime closure |
| `gen_brand_assets.py` | Derives the transparent PNGs and icons the app loads from the source logo artwork in `assets/brand/` |

`lib/` holds shared shell helpers (`deploy-env.sh`, `version.sh`) sourced by the
scripts above; it is not run directly.
