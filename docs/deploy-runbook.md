# Deploy runbook (audr stack on the self-hosted deploy host)

This repository is public, so it records no host addresses. Throughout this
document `$AUDR_DEPLOY_HOST` is the `user@host` of the deploy host and
`$AUDR_REGISTRY` the `host:port` of the image registry; both come from an
untracked `deploy.env` at the repo root (copy `deploy.env.example`) or from the
environment, and the CI workflows read the registry from the Gitea repository
variable `AUDR_REGISTRY`.

The deploy pipeline is a single guarded Gitea Actions job, `.gitea/workflows/deploy.yaml`.
The file you edit is `ci/gitea-overlay/workflows/deploy.yaml` in this repository,
and it is the copy that **runs**: `~/bin/audr-github-mirror.sh` reads it out of
the GitHub `main` commit it is mirroring and bakes it into the Gitea `main` commit
as `.gitea/workflows/deploy.yaml`. A workflow change therefore ships like any
other change — commit, push to `main`, wait for the next mirror tick (15 min).

They are deliberately not tracked at `.gitea/workflows/` because the mirror pushes
every non-`main` branch verbatim, and Gitea would then replay the suite for every
stale branch it syncs (see `ci/gitea-overlay/README.md`).

The host overlay directory (`~/.config/audr-mirror/overlay/.gitea/workflows/`) is
now only a **fallback** for files the commit does not carry; it cannot override
one that it does. `scripts/sync-ci-overlay.sh --check` still diffs the two and
exits 1 on a difference — worth running if you suspect a host-only file is in
play, but no longer a step in shipping a workflow change. Before AUD-443 the host
copy won, and a forgotten sync silently ran a stale `deploy.yaml` that took the
stand down for two hours.

## Invariants the pipeline depends on

**The deploy runs two compose files.** `compose.yaml` is the public install: it
pulls `ghcr.io/br3d/audr-backend:<version>` literally and knows nothing about
our registry. `compose.deploy.yaml` is the overlay that redirects the three
backend services at `${AUDR_REGISTRY}/audr-backend:${BACKEND_TAG}`, and both
variables are required — a missing one fails the deploy instead of silently
pulling the public release. The deploy job and `scripts/deploy.sh` copy both
files into `~/audr/` and pin

    COMPOSE_FILE=compose.yaml:compose.deploy.yaml

in `~/audr/.env`, so a **manual** `docker compose` run in that directory
resolves the same images the pipeline deployed. If a hand-run compose command
there ever tries to pull from `ghcr.io`, that line is missing (AUD-439).

**Images are pinned, never floated.** The overlay resolves `${BACKEND_TAG}`
from `~/audr/.env`, and the deploy job pins it to the build sha. This is not
cosmetic: `docker compose up -d` decides whether to recreate a service by
comparing the service *definition*, not the image ID a floating tag currently
resolves to. While the deployed image was `...:latest`, every deploy
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

## Deploying by hand with `scripts/deploy.sh`

The pipeline is the normal path; `./scripts/deploy.sh` is the same deploy driven
from a workstation, for when the runner is unavailable. It needs the SSH key at
`./id_ed25519` (mode `600`) and Docker Engine 26+ on the deploy host, and it:

1. Copies `compose.yaml` and the private-registry overlay `compose.deploy.yaml`
   to the host over SCP, and pins
   `COMPOSE_FILE=compose.yaml:compose.deploy.yaml` in the remote `.env` so
   manual compose commands there resolve the same images
2. Pulls the current images from `$AUDR_REGISTRY`
3. Runs `alembic upgrade head` via the `migrate` service
4. Restarts the stack with `docker compose up -d --remove-orphans`
5. Polls `/health` until healthy (60 s timeout)

This is our deployment of our instance, not a self-hosting procedure — an
operator running their own instance has no access to this registry and installs
from `compose.yaml` alone, as [operations.md](operations.md) describes.

## Stand down after a failed deploy

A red deploy usually means nothing is wrong with the stand: most of the guarded
job's failures happen before anything destructive, and the ones that do not
normally end in a rollback that leaves the previous release serving. So the run
status alone does not tell you whether to drop everything.

The **Report stand state (failure path)** step at the end of every failed deploy
answers exactly that question, and since AUD-444 it also sends the DOWN verdict
to Telegram so you do not have to be looking at the run to learn about it (see
[Alerting](#alerting-how-you-find-out-the-stand-is-down)). Read it first:

- `[stand] deploy FAILED but the stand is serving` — no outage. Fix the build or
  the gate at human speed.
- `[stand] DEPLOY FAILED AND THE STAND IS DOWN` — audr is not answering. Act now.

That second line is the one AUD-443 did not have. Run 447 failed correctly, the
rollback could not recover because it resolved images through the same broken
compose, and the stand served nothing but Postgres for two hours because the job
log ended at `migrate` exiting 255 and nobody read further.

When the stand is down, the cause is almost always one of two things:

1. **Compose resolved the wrong image.** Since AUD-443 the deploy asserts this
   up front, so a current pipeline fails in a second with `the
   compose.deploy.yaml overlay did not take effect`. On the host, confirm with
   `cd ~/audr && docker compose config --images` — every `audr-backend` line
   must be `$AUDR_REGISTRY/audr-backend:<tag>`, never `ghcr.io/...`. If it is
   not, check `COMPOSE_FILE=compose.yaml:compose.deploy.yaml` in `~/audr/.env`
   and that both compose files are present.
2. **The schema and the image disagree** — the next section.

Note that 1 *causes* 2: the public image's alembic tree lags the live DB, so a
silent downgrade presents as a migration failure. Fix the image resolution
before touching the schema; downgrading a schema to match an image that was
never meant to run is how a bad deploy becomes a bad database.

## Alerting: how you find out the stand is down

AUD-443's two hours were not deploy time, they were nobody-noticing time. Two
independent paths now push that fact out of a log and onto a phone, and they
cover deliberately different failure shapes:

| | fires when | threshold | lives in |
|---|---|---|---|
| CI failure step | a deploy fails **and** leaves `/health/ready` not-ok | none — immediate | `ci/gitea-overlay/workflows/deploy.yaml` |
| `audr-watchdog.timer` | `/health/ready` fails 5 probes in a row, deploy or no deploy | ~5 min sustained | `scripts/stand-watchdog.sh` on the host |

The watchdog is the one that matters most, because the CI step structurally
cannot see an outage that no deploy caused — an OOM kill, a reboot, Postgres
dying on its own produce no CI run at all. It probes every 60s, alerts on the
fifth consecutive failure, repeats hourly while still down, and sends one
`✅ RECOVERED` message when `/health/ready` returns ok again so the responder
knows to stand down.

It does not fire on the first failed probe on purpose: a single miss is
routinely just a container restarting mid-deploy, and paging on it teaches the
one person on call to mute the channel, which costs more than the five minutes
it saves. Target human response is 15 minutes.

**Channel: Telegram.** Chosen because the stand is on a LAN address — Telegram
needs one outbound HTTPS POST and nothing listening, whereas healthchecks.io or
any hosted prober needs ingress to `192.168.1.228` plus a firewall change just
to receive a heartbeat.

### Credentials

Both paths call `scripts/notify.sh`, which reads `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` from the environment or from `~/audr/secrets/telegram.env` on
the deploy host. One file, one copy to rotate, and nothing in this repository —
it is public. `secrets/` is gitignored.

**`notify.sh` with no credentials logs the message and exits 0.** That is
deliberate: every caller is already on a failure path, and an alerting helper
that turns "the stand is down" into "the alerting helper also broke" is worse
than none. The consequence is that the code paths all run on a fresh clone, and
that activating the channel is purely dropping the file in place — no code
change, no redeploy:

```bash
# 1. @BotFather -> /newbot, keep the token.
# 2. Send the bot any message, then read the chat id from
#    https://api.telegram.org/bot<TOKEN>/getUpdates  ->  result[].message.chat.id
# 3. On the deploy host:
umask 077
cat > ~/audr/secrets/telegram.env <<'CREDS'
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=987654321
CREDS
# 4. Prove delivery. --strict makes an unconfigured/undelivered send exit 1.
~/.local/share/audr-watchdog/scripts/notify.sh --strict "audr alerting test"
```

### Installing and inspecting the watchdog

```bash
scripts/install-watchdog.sh            # install or update, then show status
scripts/install-watchdog.sh --check    # what is installed, and whether creds exist
```

The installer copies the two scripts to `~/.local/share/audr-watchdog/scripts/`
and renders the unit from `ci/host-units/audr-watchdog.service.in`. The copy is
the point: the only git checkout on that host belongs to the CI runner, which
rewrites it every deploy and resets it on a failed one, so a watchdog pointed at
it would lose its code exactly when it is needed.

**Editing `scripts/stand-watchdog.sh` or `scripts/notify.sh` does not reach the
host until `install-watchdog.sh` runs again.** This is the opposite of the
workflows, which ship with the commit since AUD-443 — remember the difference.

```bash
systemctl list-timers audr-watchdog.timer      # next/last probe
sudo systemctl start audr-watchdog.service     # probe right now
cat ~/.local/state/audr-watchdog/consecutive-failures   # 0 when healthy

# Probe history and alert decisions. `sudo` is required: the unit runs as
# codex but logs to the system journal, and plain `journalctl -u` as codex
# prints "No entries" rather than an error, which reads exactly like a dead
# timer. One healthy tick logs "[watchdog] ok".
sudo journalctl -u audr-watchdog.service -n 50
```

To silence it during planned maintenance, `sudo systemctl stop
audr-watchdog.timer` — and start it again afterwards. Stopping the timer is
better than raising the threshold, because the counter file resets on the first
healthy probe either way.

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
older than 48h, caps how many unused release images may stay resident
regardless of age, escalates to much shorter windows when the root filesystem
is still ≥80% full afterwards, and always exits 0 — a GC problem must never
fail an otherwise healthy deploy. Confirm it ran by looking for
`[host-gc] after: ...` in the job log.

```bash
# ad-hoc run: copy it over, since the runner's checkout dir is not stable
scp -i id_ed25519 scripts/host-gc.sh "$AUDR_DEPLOY_HOST:/tmp/"
ssh -i id_ed25519 "$AUDR_DEPLOY_HOST" 'bash /tmp/host-gc.sh --dry-run'   # report only
ssh -i id_ed25519 "$AUDR_DEPLOY_HOST" 'IMAGE_MAX_AGE=24h CACHE_MAX_AGE=24h bash /tmp/host-gc.sh'
```

### Age alone is rate-blind — the count cap and the escalation

An age window retains "two days of deploys", which is a handful of images on a
quiet day and well over a dozen on a busy one. On 2026-10-04 (AUD-425) the host
was back at 80% with ~20 release images (~10GB) *all younger than the 48h
window*: GC ran on every merge, found nothing eligible, and logged its own warn
threshold rather than acting on it — the AUD-379 failure mode from the other
side. Two additions make retention pressure-aware without tightening the
relaxed default:

* **`IMAGE_KEEP_COUNT`** (default 5) — at most this many *unused* `audr-*`
  images stay resident, newest first, however young. Images a container
  references and whatever `:latest` and `:rollback` point at are excluded from
  the cap entirely, so the live stack and the no-pull rollback are never the
  thing trimmed. Removal uses `docker image rm` without `-f`, so a referenced
  image is refused rather than yanked even if that protected set were wrong.
* **Automatic escalation** — if `/` is still ≥ `WARN_PCT` after the normal
  pass, the script immediately repeats it with `ESCALATE_CACHE_MAX_AGE` /
  `ESCALATE_IMAGE_MAX_AGE` (6h) and `ESCALATE_IMAGE_KEEP_COUNT` (2), logging
  `escalating: ...` so the job log says it happened. If the filesystem is
  *still* over the line after that, Docker is no longer what is filling the
  disk, and the second warning says so.

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
