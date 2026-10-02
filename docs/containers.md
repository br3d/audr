# Containers: what each one is for, and whether we need it

Written for AUD-386 — "мне не нравится, что у нас такое большое количество
контейнеров". The short answer: the number is smaller than it looks, one
service was genuinely redundant and has been removed, and one more could go if
we accept a trade-off that is spelled out at the bottom.

## What `compose.yaml` defines

Five services, but only **four of them are containers that keep running**.

| Service   | Image            | Lifetime        | Why it exists |
|-----------|------------------|-----------------|---------------|
| `db`      | `postgres:16-alpine` | long-running | The database. Holds wallets, balances, the asset catalog, price history, job schedules, and the wrapped master key. |
| `migrate` | `audr-backend`   | **one-shot**    | Bootstrap: `alembic upgrade head`, then `audr.operations.init_key`. Exits 0 and stays exited. |
| `api`     | `audr-backend`   | long-running    | The FastAPI HTTP API (`uvicorn`) on port 8000. Serves `/api/*` and `/health/*`. Not published to the host — only `web` talks to it. |
| `worker`  | `audr-backend`   | long-running    | The background job runner (`python -m audr.jobs`): token discovery, balance scans, quote refresh, on-chain event indexing, news refresh, RPC/quote validation. |
| `web`     | `nginx:1.27-alpine` | long-running | Serves the built SPA on port 80 and reverse-proxies `/api/*` and `/health/*` to `api`. This is the only service with a published port. |

So `docker ps` on a healthy deployment shows **four** containers, not five:

```
audr-web-1      audr-frontend   Up
audr-worker-1   audr-backend    Up
audr-api-1      audr-backend    Up (healthy)
audr-db-1       postgres:16     Up (healthy)
```

Note also that three of the five services share **one image** (`audr-backend`) —
they are the same build invoked with three different commands. The image is
pulled and stored once.

## Why `migrate` is a separate service rather than part of `api` startup

Deliberate, and it is paid for by a real outage. The deploy pipeline runs
`docker compose run --rm -T migrate` as its own step, so a failed migration
fails the deploy *before* any application container is recreated, and the
rollback is clean. If migrations ran inside the `api` entrypoint instead, a bad
migration would mean a crash-looping api container against a half-migrated
database — much harder to roll back.

It is also pinned to `restart: "no"`. `api` and `worker` wait on it with
`service_completed_successfully`, so an unbounded restart policy turns a failed
migration into a `docker compose up` that blocks forever. That is exactly what
happened on 2026-09-29: the deploy job hung 2h18m mid-rollback and held the
`audr-deploy` lock, starving every queued build.

## What was removed in AUD-386

There used to be a sixth service, `init`, which ran
`python -m audr.operations.init_key` as a second one-shot chained after
`migrate`. It was redundant: same image, same database, and `init_key` is
idempotent by design (its own docstring says "safe to call on every startup").
The split bought nothing but an extra compose service and an extra container
start per deploy.

It is now the second half of the `migrate` command:

```yaml
command:
  - sh
  - -c
  - "python -m alembic upgrade head && python -m audr.operations.init_key"
```

Order still matters — `init_key` writes to `key_state`, which the baseline
migration creates — hence `&&` rather than two parallel commands.

## Does rotki have this many containers?

No. rotki ships **one** container:

```bash
docker run -d --name rotki -p 8084:80 \
    -v $HOME/.rotki/data:/data -v $HOME/.rotki/logs:/logs \
    rotki/rotki:latest
```

Inside it, a supervisor runs nginx plus the Python backends, which listen only
on loopback. Their published docker-compose examples add containers only for
things outside the app itself — Traefik for TLS termination, Watchtower for
auto-updates.

Two structural reasons rotki gets away with one container, and what they cost
them:

1. **rotki has no database server.** It uses SQLite/SQLCipher files on a mounted
   volume. That removes the `db` container outright. We chose PostgreSQL —
   which is why `db` exists and is not negotiable without changing the storage
   engine. This is the single biggest reason for our count.
2. **rotki runs its background tasks as greenlets inside the API process**,
   not as a separate process. That is why they have no `worker`.

A fair comparison is therefore: rotki 1 container because it is SQLite-backed
and single-process; audr 4 because it is Postgres-backed with a separate job
runner and a separate static-asset server.

## Could we go lower than four?

Two candidates, in order of how easy they are.

### `web` → fold into `api` (4 containers → 3)

This is the cheap one, and it is already half-built: the `runtime` stage of the
`Dockerfile` **already copies the built frontend into `/app/static`**:

```dockerfile
COPY --from=frontend-builder /app/dist/ /app/static/
```

Nothing mounts it. So today we build the same SPA bundle into two images and
ship it twice, and only the nginx copy is ever served. Either that `COPY` is
dead weight and should go, or it is the consolidation path and nginx should go.

Serving the SPA from FastAPI (`StaticFiles` + an index fallback for client-side
routes) would drop the `web` container and the separate `audr-frontend` image,
and would make the deploy publish `api` on port 80 directly. This is also
exactly rotki's shape.

The trade-off: nginx is better at static serving than FastAPI — gzip/brotli,
cache headers, byte ranges, not occupying an application worker to hand back a
JS bundle. For a single-user self-hosted instance this is very unlikely to
matter. For a publicly exposed one it might.

### `worker` → fold into `api` (3 containers → 2)

Possible — rotki does it — but this is the one I would not rush. Keeping the job
runner in its own process means a long RPC scan or a stuck provider call cannot
starve HTTP request handling, and `docker logs audr-worker-1` is how we debug
job behaviour today. It also lets us restart the worker without dropping the
API. The container count saving is one; the operational cost is real.

### Not candidates

- `db` — required unless we migrate off PostgreSQL. Not worth reopening.
- `migrate` — a one-shot that is not running in steady state, and the deploy
  safety argument above is worth more than the line in `compose.yaml`.
