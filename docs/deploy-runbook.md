# Deploy runbook (audr stack on 192.168.1.228)

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

**Images are pinned, never floated.** `compose.yaml` resolves
`${BACKEND_TAG}` / `${FRONTEND_TAG}` from `~/audr/.env`, and the deploy job pins
both to the build sha. This is not cosmetic: `docker compose up -d` decides
whether to recreate a service by comparing the service *definition*, not the
image ID a floating tag currently resolves to. While the compose file said
`image: ...:latest`, every deploy retagged `:latest`, ran the migrations, passed
the health-gate **against the previous containers** and reported `Deploy OK`
while the running code never changed. Step 5b of the job now asserts that
`audr-api-1`, `audr-worker-1` and `audr-web-1` really run the new tag before the
health-gate result is trusted.

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
`api`/`worker`/`init` stuck in `Created`, nginx serves 502.

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
  192.168.1.90:8085/audr-backend:<image-with-the-newer-revisions> \
  python -m alembic downgrade <target-rev>
# …or roll forward by pinning BACKEND_TAG/FRONTEND_TAG to an image whose
# migration tree contains the DB's revision. Check the downgrade is safe first
# (a table the downgrade drops must be empty).

# 4. Bring the stack back and verify with the same three signals as the gate.
cd ~/audr && timeout --foreground 300 docker compose up -d --remove-orphans
curl -s http://localhost/health/ready   # must contain "status":"ok"
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/
```

`GET /health/ready` is the only trustworthy public signal: nginx proxies just
`^/(api|health)/` and serves `index.html` for everything else, so a bare `/`
returns 200 from the SPA fallback even when the API is dead.

## Registry tag retention

The registry at `192.168.1.90:8085` is a plain CNCF `distribution` registry
behind nginx — **not Harbor**, despite the name used in older notes. It exposes
only the `/v2` API (`/api/v2.0/systeminfo` 404s), so there is no retention
feature to switch on: retention is enforced from outside by
`scripts/registry-prune.py`, which needs nothing but HTTP.

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
- **A sha must be kept in both repositories or neither.** A deploy pins
  `BACKEND_TAG` and `FRONTEND_TAG` to the same sha, so keeping it on one side
  gives a rollback that half-succeeds. Per-repo age ranking does not deliver
  that by itself: many tags here share an identical image `created` timestamp
  (a rebuild of unchanged layers reuses the date) and the resulting ties break
  differently per repository. The script therefore unions each repository's
  newest-N and applies that union everywhere.

The script refuses any repository not named `audr-*`: this registry is shared
with an unrelated project (`svetu-backend`, `svetu-frontend`, 91 tags each).

**Deleting tags does not free disk.** A manifest delete only unlinks; the blobs
are reclaimed solely by

```bash
registry garbage-collect -c /etc/docker/registry/config.yml   # on 192.168.1.90
```

which must run as a process on the registry host. We have no shell on
`192.168.1.90`, so that half stays open and is tracked separately — until it
runs, pruning buys catalog clarity, not bytes. The same gap is why
`library/aud-pushtest` still appears in `/v2/_catalog` with `tags: null`.
