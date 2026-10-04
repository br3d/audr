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
| `release.sh` | Cuts a semantic-version release: rewrites the version everywhere it appears, commits, and creates the annotated tag. Pushing is opt-in (`--push`) because a pushed `v*` tag triggers the deploy pipeline |
| `sync-ci-overlay.sh` | Reconciles `ci/gitea-overlay/workflows/` with the deploy host's mirror overlay, which is where the Gitea Actions files actually run from |

## Internal — maintenance and generated files

| Script | What it does |
|---|---|
| `host-gc.sh` | Bounds the deploy host's Docker disk usage. Every push to `main` builds a new image on the host runner and nothing else removes them |
| `registry-prune.py` | Enforces a tag-retention policy on the image registry over the `/v2` HTTP API, since the registry has no built-in retention and we have no shell on it |
| `test_registry_prune.py` | Dependency-free unit tests for the pruner: `python3 scripts/test_registry_prune.py`. Not in the pytest suite, which runs in a container that cannot see `scripts/` |
| `benchmark.py` | Two reports: warm dashboard/history read latency against the reference scale fixture, and the catalog's logical RPC call count. Results are published in [`docs/benchmark.md`](../docs/benchmark.md) |
| `gen_third_party.py` | Regenerates the dependency licence tables in [`docs/third-party.md`](../docs/third-party.md) from the locked runtime closure |
| `gen_brand_assets.py` | Derives the transparent PNGs and icons the app loads from the source logo artwork in `assets/brand/` |

`lib/` holds shared shell helpers (`deploy-env.sh`, `version.sh`) sourced by the
scripts above; it is not run directly.
