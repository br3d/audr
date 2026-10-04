# Deploy runbook (audr stack on the self-hosted deploy host)

This repository is public, so it records no host addresses. Throughout this
document `$AUDR_DEPLOY_HOST` is the `user@host` of the deploy host and
`$AUDR_REGISTRY` the `host:port` of the image registry; both come from an
untracked `deploy.env` at the repo root (copy `deploy.env.example`) or from the
environment, and the CI workflows read the registry from the Gitea repository
variable `AUDR_REGISTRY`.

The deploy pipeline is a single guarded Gitea Actions job, `.gitea/workflows/deploy.yaml`.
The copy that actually **runs** lives in the Gitea mirror overlay on the deploy
host (`~/.config/audr-mirror/overlay/.gitea/workflows/`);
`~/bin/audr-github-mirror.sh` bakes it into the Gitea `main` commit each time
GitHub `main` moves. Editing the overlay therefore has no effect until the next
push to GitHub `main`.

The **versioned source of record** for those files is `ci/gitea-overlay/` in this
repository. They are deliberately not tracked at `.gitea/workflows/`, because the
mirror pushes every non-`main` branch verbatim and Gitea would then replay the
suite for every stale branch it syncs (see `ci/gitea-overlay/README.md`). Keep the
two copies in agreement with `scripts/sync-ci-overlay.sh`; `--check` diffs without
writing and exits 1 on drift, which is the first thing to run when the pipeline
misbehaves after a workflow was edited on the host.

## Invariants the pipeline depends on

**Images are pinned, never floated.** `compose.yaml` resolves `${BACKEND_TAG}`
from `~/audr/.env`, and the deploy job pins it to the build sha. This is not
cosmetic: `docker compose up -d` decides whether to recreate a service by
comparing the service *definition*, not the image ID a floating tag currently
resolves to. While the compose file said `image: ...:latest`, every deploy
retagged `:latest`, ran the migrations, passed the health-gate **against the
previous containers** and reported `Deploy OK` while the running code never
changed. Step 5b of the job now asserts that `audr-api-1` and `audr-worker-1`
really run the new tag before the health-gate result is trusted.

There is one image and one tag variable since AUD-388 removed the
`audr-frontend` image. A stale `FRONTEND_TAG=` line may still sit in
`~/audr/.env`; nothing interpolates it, and it is safe to delete or ignore.

**A rollback must restore the schema, not just the images.** Restoring images
alone is not a restorable state: a deploy that migrated the DB and then failed
leaves the schema ahead of the old image, whose `alembic upgrade head` dies with
`Can't locate revision identified by '<new>'`. The job snapshots the pre-deploy
alembic revision and, on failure, downgrades back to it **using the new image**
(the only one that knows how to reverse its own revisions) before restoring the
tags. If that downgrade is impossible the old tags are deliberately *not*
restored and the stack stays on the new build — a running-but-newer stack beats
a guaranteed-dead one — and the job fails loudly for a human decision.

**One-shots must not restart.** `migrate` and `init` are `restart: "no"`.
`init`/`api` wait on them with `service_completed_successfully`, so an unbounded
`restart: on-failure` turns a failed migration into a `docker compose up` that
blocks forever instead of failing.

## Three CI-only gotchas, each of which has caused an outage or a silent no-op

- **`timeout --foreground`** — plain GNU `timeout` puts the child in a new
  process group, so a docker CLI that writes to the terminal takes `SIGTTOU` and
  is *stopped* (`ps` state `T`), hanging the job forever: the exact failure the
  timeout was added to prevent.
- **`docker compose run -T`** — never allocate a TTY in a CI job.
- **Exported variables beat `.env`** — the job sources `.env` for `DB_PASSWORD`,
  which exports the *old* `BACKEND_TAG`. Compose gives the environment
  precedence over the `.env` file, so rewriting the file alone leaves compose
  resolving the stale tag and skipping the recreate. `env_set` writes the file
  **and** exports.

## Recovering a schema/image mismatch by hand

Symptom: `migrate` crash-loops with `Can't locate revision identified by 'NNNN'`,
`api`/`worker` stuck in `Created`, and port 80 refuses connections (there is no
proxy container left to answer with a 502 — the thing that publishes :80 *is*
the `api` container).

```bash
# 1. If a deploy job is wedged, kill it — a blocked `compose up` holds the
#    audr-deploy concurrency group and starves every queued build.
ps -eo pid,etime,cmd | grep -E 'workflow/[0-9]+\.sh|docker compose'

# 2. Find where the DB actually is, and which local image knows that revision.
docker exec audr-db-1 psql -U audr -d audr -tAc 'select version_num from alembic_version'
docker run --rm --entrypoint sh <candidate-image> -c 'ls /app/migrations/versions'

# 3. Either roll the schema back to match the image that is meant to run…
set -a; . ~/audr/.env; set +a
docker run --rm --network audr_internal -w /app \
  -e DATABASE_URL="postgresql+psycopg://audr:${DB_PASSWORD}@db:5432/audr" \
  "$AUDR_REGISTRY/audr-backend:<image-with-the-newer-revisions>" \
  python -m alembic downgrade <target-rev>
# …or roll forward by pinning BACKEND_TAG to an image whose migration tree
# contains the DB's revision. Check the downgrade is safe first (a table the
# downgrade drops must be empty).

# 4. Bring the stack back and verify with the same four signals as the gate.
cd ~/audr && timeout --foreground 300 docker compose up -d --remove-orphans
curl -s http://localhost/health/ready   # must contain "status":"ok"
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/
curl -s http://localhost/api/v1/version # version + commit actually running
```

After a manual recovery the live version is deliberately *not* the one this
checkout carries, so pin the expectation when smoke-testing it:

```bash
AUDR_EXPECT_VERSION=<the version you rolled back to> ./scripts/smoke-test.sh
```

See `docs/releases.md` for the versioning and image-tagging scheme.

`GET /health/ready` is the trustworthy public signal, because it performs a real
DB and master-key check inside FastAPI rather than merely proving something is
listening. Since AUD-388 a 200 on `/` is also meaningful — the SPA is served by
the `api` container, so it cannot answer at all while the API is down — but it
still says nothing about the database, which is the usual thing broken here.

## Deploy host disk retention

The host runner is also the deploy host, so every push to `main` mints a new
~500MB `audr-backend:<sha>` image on the same 32GB root filesystem that carries
the live stack's database volume. Nothing removed them until AUD-395: by
2026-10-03 the host held 299 images (14.9GB) plus 3.8GB of build cache at 89%
full — about four more deploys before a build would have hit ENOSPC, taking the
database down with it rather than failing politely.

`scripts/host-gc.sh` now runs as a `if: always()` step at the end of both
`deploy.yaml` and `build.yaml`. It prunes build cache and unreferenced images
older than 48h, warns when the root filesystem is still ≥80% full afterwards,
and always exits 0 — a GC problem must never fail an otherwise healthy deploy.
Confirm it ran by looking for `[host-gc] after: ...` in the job log.

```bash
# ad-hoc run: copy it over, since the runner's checkout dir is not stable
scp -i id_ed25519 scripts/host-gc.sh "$AUDR_DEPLOY_HOST:/tmp/"
ssh -i id_ed25519 "$AUDR_DEPLOY_HOST" 'bash /tmp/host-gc.sh --dry-run'   # report only
ssh -i id_ed25519 "$AUDR_DEPLOY_HOST" 'IMAGE_MAX_AGE=24h CACHE_MAX_AGE=24h bash /tmp/host-gc.sh'
```

Pruning this host cannot disarm rollback. `deploy.yaml`'s snapshot step pushes
`:rollback` to the registry precisely so the rollback path does not depend on
the host's local cache surviving a prune, and `restore_image()` recovers in
three steps: local, then the immutable per-sha tag in the registry, then
`:rollback`. Host-local images are a cache, not the source of truth. The 48h
window is belt-and-braces on top of that, keeping the previous release resident
so the common rollback needs no pull at all; `docker image prune` never
considers an image a running container references.

### CI test images are the other accumulator

Release images are not the only thing that grows. `ci.yaml` namespaces its
compose project per run (`audr-test-ci-<run>-<attempt>`, from AUD-310, so
concurrent runs cannot destroy each other's database), which means the images
compose builds are named per run too — about 1GB of `-backend-tests` plus
`-migrate-test` on every CI run. A unique name can never be a cache hit for a
later run, so they are garbage the moment the job ends, yet `docker compose
down -v` removes containers, volumes and networks but *not* images. Under the
48h release window alone they outlived their usefulness by two days: the first
measurement after the GC step shipped found 27 such images from 16 past runs
still resident, and CI pushed the host from 44% to 60% in a couple of hours.

So they are deleted at the source: `ci.yaml`'s teardown step removes the images
matching its own `$COMPOSE_PROJECT-*` — scoped to that run, so a concurrent CI
job on the other runner slot is untouched. `host-gc.sh` then reaps any
`audr-test-ci-*` image older than `TEST_IMAGE_MAX_AGE_HOURS` (default 6) as a
second line of defence, for the case where the runner was killed before its
teardown step could run. That reaper uses `docker image rm` *without* `-f`, so
an image a container still references is refused rather than pulled out from
under a live run.

## Registry tag retention

The registry (`$AUDR_REGISTRY`) is a plain CNCF `distribution` registry
behind nginx — **not Harbor**, despite the name used in older notes. It exposes
only the `/v2` API (`/api/v2.0/systeminfo` 404s), so there is no retention
feature to switch on: retention is enforced from outside by
`scripts/registry-prune.py`, which needs nothing but HTTP.

Since AUD-345 that script is **not something you have to remember to run**:
`deploy.yaml`'s `Enforce registry tag retention` step runs it with `--apply` at
the end of every *successful* deploy, on the deploy host, so the default
`--env-file` is the live `~/audr/.env` and the deployed tag is protected from
the inside. Two deliberate asymmetries with the `host-gc.sh` step next to it:

- it is **not** `if: always()`. After a failed deploy the stack has just been
  rolled back and `:rollback` is the only thing between us and an outage — the
  worst possible moment to delete from the registry, and no bytes would be
  freed anyway.
- it exits 0 even when the pruner fails. Retention is hygiene on a healthy
  deploy and must never turn a green deploy red; look for
  `[registry-prune] FAILED` in the job log.

Run it by hand for a dry run, a one-off `--keep`, or the orphaned
`audr-frontend` repository:

```bash
python3 scripts/registry-prune.py                 # dry run; prints keep/prune per tag
python3 scripts/registry-prune.py --apply         # delete
python3 scripts/registry-prune.py --keep 12 --protect b57f2d107ecc
python3 scripts/test_registry_prune.py            # retention-rule unit tests, no deps
```

A tag survives if it is `latest`/`rollback`, looks like a release tag (`v0.1.0`),
is the tag pinned in `~/audr/.env`, is among the `--keep` newest by image
creation time, or shares a manifest digest with anything protected by those
rules. Three of those deserve explaining, because each one is load-bearing:

- **Deletion is by digest, and a digest delete removes every tag pointing at
  it.** Pruning a stale sha tag that happens to share `latest`'s or
  `rollback`'s digest would destroy the rollback path, so digests shared with a
  protected tag are excluded. (`f4c1636` survives today purely because it is
  `v0.1.0`'s digest.)
- **The live and previous sha tags are not history.** `deploy.yaml`'s
  `rollback()` restores the previous release by `docker pull`ing the immutable
  `audr-backend:<prev-sha>` from this registry, falling back to `:rollback`. A
  host prune or rebuild is exactly when that pull matters.
- **A sha must be kept in both repositories or neither.** Moot since AUD-388
  left `audr-backend` as the only pruned repository, but still what makes any
  multi-repo run safe. Back when a deploy pinned `BACKEND_TAG` and
  `FRONTEND_TAG` to the same sha, keeping it on one side gave a rollback that
  half-succeeded. Per-repo age ranking does not deliver symmetry by itself:
  many tags here share an identical image `created` timestamp (a rebuild of
  unchanged layers reuses the date) and the resulting ties break differently
  per repository. The script therefore unions each repository's newest-N and
  applies that union everywhere. The orphaned `audr-frontend` repository is no
  longer pruned by default; clear it deliberately with
  `--repo audr-frontend` if you want the catalog tidy.

The script refuses any repository not named `audr-*`: this registry is shared
with an unrelated project (`svetu-backend`, `svetu-frontend`, 91 tags each).

**Deleting tags does not free disk.** A manifest delete only unlinks; the blobs
are reclaimed solely by

```bash
registry garbage-collect -c /etc/docker/registry/config.yml   # on the registry host
```

which must run as a process on the registry host. We have no shell there,
so that half stays open and is tracked separately — until it
runs, pruning buys catalog clarity, not bytes. The same gap is why
`library/aud-pushtest` still appears in `/v2/_catalog` with `tags: null`.
