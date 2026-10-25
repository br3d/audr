# Scripts

Thirty-two scripts live here, but an operator only ever runs five of them. The
rest are the team's build, release, alerting and maintenance machinery — they
assume a registry, a deploy host or an SSH key that a self-hosting user does
not have.

## If you are running audr

| Script | What it does |
|---|---|
| `setup-secrets.sh` | One-time credential initialisation for a fresh install: generates `.env` (`DB_PASSWORD`, `SECRET_KEY`) and keeps a recovery copy of the key in `secrets/master_key.hex`. Idempotent — safe to re-run, never overwrites |
| `backup.sh` | Encrypted Postgres backup. Dumps the `db` service and pipes it straight through encryption, so the dump never touches disk in the clear. Writes a timestamped archive to `./backups` (or `$AUDR_BACKUP_DIR` / `$1`). Copies the KEK out separately, resolved from `secrets/master_key.hex` or, where that does not exist, `.env`'s `SECRET_KEY`; `AUDR_BACKUP_SKIP_KEY_COPY=1` suppresses the copy |
| `restore.sh` | Restores an archive produced by `backup.sh`. Restores **data only** — see the header comment about `SECRET_KEY` / `secrets/master_key.hex`. Needs `--force` to overwrite a database that already has tables |
| `install-backup.sh` | Installs the daily encrypted dump on the deploy host: pinned script copies under `~/.local/share/audr-backup/`, `audr-backup.timer`/`.service` from `ci/host-units/`. Idempotent; `--check` reports without changing anything. Covers volume loss and bad migrations, **not** loss of the host — see [`docs/operations.md#backups`](../docs/operations.md#backups) |
| `seed_dev.sh` | Fills a running instance with the canonical demo fixtures: owner account, a sample wallet, and the RPC integration when an RPC URL is available. Useful for trying audr out before connecting your own wallets |

Backup, restore and first-run setup are all documented in context in
[`docs/operations.md`](../docs/operations.md); prefer that over reading the
scripts.

## Internal — build, release, deploy

These require `deploy.env` (registry and deploy-host addresses, never tracked)
or an SSH key, and they act on infrastructure outside your installation.

| Script | What it does |
|---|---|
| `test.sh` | The merge gate: ruff, backend pytest, mypy-vs-baseline and frontend Vitest in Docker Compose. `--backend-only` / `--frontend-only`. The static gates were CI-only jobs until AUD-744, so a locally-green merge could land ruff- or mypy-red on `main` — and twice did; `AUDR_SKIP_STATIC=1` skips them, and the GitHub `tests` job sets it because `lint` and `backend-mypy` run there as their own jobs |
| `prewarm-images.sh` | `test.sh`'s first phase: pulls the external base images the run needs — resolved from `compose.test.yaml` and the Dockerfile `FROM` lines, never a hardcoded list — before any suite phase can need one. Skips anything already in the local store, so it costs nothing on a warm box; retries a refused pull, because Docker Hub's anonymous allowance is per source IP and this host shares it with every other agent and the Gitea runner. Exists so a 429 reds a phase about pulling instead of being read as a broken lockfile, which is how it was first misdiagnosed (AUD-747). `AUDR_SKIP_PREWARM=1` restores lazy pulling; `--list` just prints the refs |
| `ci-phase-ledger.sh` | Replays `test.sh`'s `.test-phases.tsv` into one GitHub step per suite phase, so a red run's *cause* is readable from job metadata — the only CI channel an agent here can read without credentials (AUD-714/AUD-715). Keep its `PHASES` list in step with `test.sh`; a unit test pairs them |
| `build.sh` | Builds the application image and pushes it to the private registry, tagged `<version>-g<short sha>` |
| `deploy.sh` | Deploys a tag to the remote host over SSH: pulls the image, runs migrations, restarts services |
| `smoke-test.sh` | Post-deploy health probe against a live instance; non-zero exit on any failed check |
| `ci.sh` | The whole pipeline end to end — build → test → deploy → smoke-test — with per-stage skip flags |
| `release.sh` | Cuts a semantic-version release: rewrites the version in the five in-repo files that carry it, commits, and creates the annotated tag. Pushing is opt-in (`--push`) because a pushed `v*` tag triggers the deploy pipeline. Deliberately does **not** touch `compose.yaml` — see `promote-image.py` |
| `promote-image.py` | Step 3 of a release: points `compose.yaml`'s `x-backend-image` at `<version>` *after* proving that tag is in GHCR and was built from the release commit, and restores the inlined healthchecks the same way. Refuses, naming the dispatch, when the publish has not run; idempotent when it has (AUD-546). Also refuses to promote a release `main` has overtaken — off-`main`, or more than 50 commits behind (`MAX_DRIFT`, raised from 20 by CEO decision in AUD-745 once 20 proved to be a 34-hour window on a multi-day human click) — because the pin decides what every fresh clone installs; `--allow-stale` overrides |
| `sync-ci-overlay.sh` | Reconciles `ci/gitea-overlay/workflows/` with the deploy host's mirror overlay. Maintenance only: the mirror reads the workflows out of the `main` commit, so this is not a step in shipping a workflow change — but it is how you prune a host-only copy the repo has deleted, which the mirror would otherwise keep baking in |

## Internal — host installers and alerting

These install or run on the deploy host only, and need the SSH key and
`secrets/` that a self-hosting install does not have.

| Script | What it does |
|---|---|
| `install-watchdog.sh` | Installs the watchdog and alert-dispatch timers/services on the deploy host, plus verbatim copies of the scripts they run. `--check` reports without changing anything |
| `stand-watchdog.sh` | Probes `/health/ready` every minute from `audr-watchdog.timer`; alerts on a sustained outage, backup staleness, disk pressure, a mute alert dispatcher, or an undrained alert spool |
| `alert-dispatch.sh` | Delivers pending critical news alerts from the stack, from `audr-alert-dispatch.timer`: reads what the worker has pending, sends it via `notify.sh`, then tells the worker to mark it delivered. `--dry-run` renders and reports without sending or marking |
| `notify.sh` | Sends an operational alert to every configured channel — a `critical` Paperclip issue (via `notify-board.py`) and/or Telegram — and spools anything it cannot deliver for `drain-alert-spool.py` to replay later |
| `notify-board.py` | Opens (and later closes) a Paperclip issue for an ongoing outage. Reached through `notify.sh`; nothing else calls it directly |
| `drain-alert-spool.py` | Replays alerts `notify.sh` could not deliver from the host, run from inside an agent so a board credential is available. `--dry-run` shows without changing anything |
| `install-mirror-dropin.sh` | Installs the boot-DNS-race drop-in for `audr-mirror.service` on the deploy host. `--check` reports without changing anything; `--check-drift` compares the host's copy against this checkout and exits non-zero if it has fallen behind |
| `host-install-drift.sh` | Asks every installer's `--check-drift` whether the deploy host still runs this checkout's pinned copies, and raises one keyed, self-resolving `notify.sh` alert when it does not. Run from the deploy workflow on every deploy (AUD-698) |
| `lib/host-install-manifest.sh` | Sourced, not run: the single source of truth for which files each installer pins to the host, and the content comparison `--check-drift` uses. Drives both the copy and the check so the two cannot disagree |
| `migration-smoke.sh` | Asserts a single Alembic head and that the newest revisions really reverse (`upgrade` → `downgrade` → `upgrade`) against the ephemeral test database. Runs as the `migration-smoke` compose service and as its own `test.sh` phase, because the guarded deploy's rollback is built on `alembic downgrade` succeeding |

## Internal — maintenance and generated files

| Script | What it does |
|---|---|
| `host-gc.sh` | Bounds the deploy host's Docker disk usage. Every push to `main` builds a new image on the host runner and nothing else removes them. Retention is age-based *and* capped by count, and escalates to shorter windows on its own while the disk is above the warn threshold |
| `registry-prune.py` | Enforces a tag-retention policy on the image registry over the `/v2` HTTP API, since the registry has no built-in retention and we have no shell on it |
| `dump-prune.py` | Applies the authorised GFS retention (N days of dumps + newest per older ISO week, optionally capped by `--weeks`) to a dump directory. Dry-run by default. Serves both dump families we own via `--prefix`: the Paperclip archive on ai-stuff (AUD-435 — the durable fix is a board setting an agent token cannot write, so this is the stopgap) and audr's own stand backups (AUD-562) |
| `backup-run.sh` | One scheduled backup: dump, **decrypt it back** to prove the archive is readable, prune to retention, and alert via `notify.sh` if any step failed — with a flag file so the next success stands the alert down. Invoked by `audr-backup.service`; safe to run by hand on the host for an off-schedule dump |
| `benchmark.py` | Two reports: warm dashboard/history read latency against the reference scale fixture, and the catalog's logical RPC call count. Results are published in [`docs/benchmark.md`](../docs/benchmark.md) |
| `gen_third_party.py` | Regenerates the dependency licence tables in [`docs/third-party.md`](../docs/third-party.md) from the locked runtime closure |
| `gen_brand_assets.py` | Derives the transparent PNGs and icons the app loads from the source logo artwork in `assets/brand/` |

`lib/` holds shared shell helpers (`deploy-env.sh`, `version.sh`) sourced by the
scripts above; it is not run directly.

## Tests for these scripts

The unit tests for the scripts here live in `backend/tests/unit/`, not beside
them. Five of them (`test_notify_board.py`, `test_drain_alert_spool.py`,
`test_registry_prune.py`, `test_promote_image.py`, `test_dump_prune.py`) were
moved there in AUD-698 after it turned out they ran in **no** job:
`backend/pyproject.toml` pins `testpaths = ["tests"]`, so nothing collected
them, and ~1,400 lines of cases — each guarding a retention rule whose failure
mode is a deleted backup or a disarmed rollback — executed only when somebody
invoked the file by hand. Four of them had in fact been failing against a
`compose.yaml` that changed under them, unnoticed.

Each resolves its subject through the repo root and needs that script
bind-mounted into the test container; `compose.test.yaml` carries one mount per
script with the reason. They remain runnable on their own
(`python3 backend/tests/unit/test_dump_prune.py`), which is how they were
designed and is still useful on a host with no Docker.
