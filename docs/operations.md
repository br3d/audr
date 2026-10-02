# Operations guide

## Quick start

### Prerequisites

- Docker Engine 26+ and Docker Compose v2
- `openssl` (for secret generation)
- The deployment SSH key at `./id_ed25519` (mode `600`) for remote deploys

### First-time setup

```bash
# 1. Generate secrets (idempotent — safe to re-run)
bash scripts/setup-secrets.sh

# 2. Start all services
docker compose up -d

# 3. Verify health
curl -s http://localhost/health/live
# {"status":"ok"}
```

`setup-secrets.sh` writes these files if they do not already exist:

| File | Purpose |
|---|---|
| `secrets/db_password.txt` | PostgreSQL password (mounted as Docker secret) |
| `secrets/master_key.hex` | 64-char hex master key-encryption-key |
| `secrets/rpc_url.txt` | Ethereum RPC URL used by `scripts/seed_dev.sh` (only written when `AUDR_SEED_RPC_URL` is set) |
| `.env` | `DB_PASSWORD` and `SECRET_KEY` for Compose |

**Never commit `.env` or `secrets/`.** Both are in `.gitignore`.

### Seeding the RPC integration (AUD-349)

The dev RPC URL embeds a provider API key, so it must never reach git.
`scripts/seed_dev.sh` resolves it at run time from the first source that is set:

1. the `AUDR_SEED_RPC_URL` environment variable,
2. `secrets/rpc_url.txt` (git-ignored, mode `600`),
3. an `AUDR_SEED_RPC_URL=...` line in `.env` (git-ignored).

```bash
# one-off: store the URL locally, then seed
AUDR_SEED_RPC_URL='https://mainnet.example/v3/<key>' bash scripts/setup-secrets.sh
./scripts/seed_dev.sh                     # or: ./scripts/seed_dev.sh http://192.168.1.228
```

The script saves the URL through `PUT /api/v1/integrations/rpc` (encrypted at
rest with the master key) and enqueues a validation run. It prints only the
hostname, never the full URL. When no URL is available the step is skipped with
a note and the rest of the seed still succeeds — the URL can then be entered
manually in Settings → Integrations. Ask infraLead for the shared value.

Seeding is unconditional: every run registers the demo Buterin wallet and
configures the RPC integration, because the script exists to give an instance
enough data to exercise the whole product. Re-runs are harmless (the wallet
address is unique-indexed, so a second run gets a `409` and changes nothing).
Pass `AUDR_SEED_WALLET=skip` when you only want the RPC step.

### Keyless RPC fallback (AUD-364)

Configuring an RPC integration is an upgrade, not a prerequisite. The chain
readers (`balance_scan`, `event_indexer`) always append the keyless public
endpoints listed in `backend/src/audr/providers/rpc_defaults.py` after whatever
is configured, and `RpcReader` moves to the next endpoint whenever one reports
itself unusable — HTTP 402 (plan exhausted), 401/403 (bad or revoked key), 5xx,
a transport failure, or a 429 that outlived its retries. The endpoint that
answers is then used for the rest of that job run; a fresh run starts from the
configured endpoint again, so a keyed provider recovers by itself once its quota
resets.

Consequences for operators:

- An exhausted Infura/Alchemy plan degrades to public endpoints instead of
  taking every chain-reading job down (the AUD-364 outage).
- A rebound hostname is still a hard failure: if the stored URL stops passing
  SSRF validation the job fails rather than silently falling back.
- `validate_rpc` deliberately does **not** fall back — it probes exactly the
  endpoint you configured, so Settings → Integrations keeps telling the truth
  about your own key.
- Public endpoints are shared infrastructure with their own unannounced rate
  limits. A sustained 402 on the configured provider is worth fixing, not
  living on.

### Keyless asset icon cache (AUD-385)

Token logos are resolved the same keyless-by-default way as RPC and quotes:
the worker tries Trust Wallet's public GitHub asset repo first, then falls
back to CoinGecko's keyless contract-lookup endpoint, and caches whichever
image it finds in the `asset_icon` table. `GET /assets/{id}/icon` only ever
serves what is already cached — the frontend falls back to a generated
monogram for anything not yet resolved, so a cold cache never slows down the
dashboard.

| Env var | Default | Purpose |
|---|---|---|
| `ASSET_ICONS_REMOTE_FETCH` | `true` | Set to `false` to stop the backend from ever contacting GitHub or CoinGecko for icons; the UI then shows monograms only. |
| `ASSET_ICON_CG_RATE_LIMIT_PER_SECOND` | `0.5` | Request budget for the keyless CoinGecko fallback (shared with the CoinMarketCap quote limiter's caution — this endpoint has a tight, unpublished per-IP quota too). |
| `ASSET_ICON_CG_RATE_LIMIT_BURST` | `1` | Burst allowance for the same limiter. |

### Subsequent starts

```bash
docker compose up -d          # start (or restart stopped containers)
docker compose logs -f api    # tail API logs
docker compose ps             # check running services
```

---

## Service topology

```
[Browser] ─── HTTP ──► [nginx :80 / web]
                              │
                 ┌────────────┴────────────┐
          /api/* │                         │ /health/*
                 ▼                         ▼
          [FastAPI :8000 / api]    [FastAPI :8000 / api]
                 │
                 ▼
         [PostgreSQL :5432 / db]
                 ▲
          [worker] ──── periodic jobs ────┘
```

Nginx (`web`) serves the compiled React SPA and reverse-proxies all `/api/*` and `/health/*` paths to the backend (`api`). The worker polls the job queue; it never binds a port.

---

## Loopback setup

All services communicate over the `internal` Docker bridge network. No service port is exposed except `web:80`. This means:

- The backend API is never reachable directly from the host — only via nginx.
- The worker connects to the database using the Docker DNS name `db`.
- For local development, the API is at `http://localhost/api/` (via nginx).

To access the backend directly for debugging without nginx:

```bash
docker compose exec api curl -s http://localhost:8000/health/live
```

---

## TLS / HTTPS proxy

The current configuration serves HTTP only. For production, place a reverse proxy (Caddy, Traefik, or nginx on the host) in front of the `web` container on port 80 and terminate TLS there.

**Caddy example** (`/etc/caddy/Caddyfile` on the host):

```
yourdomain.com {
    reverse_proxy localhost:80
}
```

Caddy handles certificate issuance and renewal automatically via Let's Encrypt.

When running behind a TLS-terminating proxy, the backend correctly reads the forwarded protocol from `X-Forwarded-Proto` (set by nginx in `nginx.conf`).

---

## Key-loss behavior

The application uses a two-layer encryption scheme:

1. **SECRET_KEY** (env var, set from `secrets/master_key.hex`): the key-encryption-key (KEK). Stored only outside the database.
2. **Master key**: a 32-byte AES-256 key generated on first boot, stored in the `key_state` table as a blob encrypted by the KEK.

The master key is used to encrypt provider credentials (RPC URLs, API keys) stored in the `settings` table.

This scheme covers credentials only — wallet addresses, holdings, and the valuation history are plaintext in Postgres. For the full at-rest picture, the threat model, and the recommended volume encryption for the `db_data` volume, see [security-at-rest.md](security-at-rest.md).

**If SECRET_KEY is lost:**

- The application will refuse to start (startup validation fails).
- The master key cannot be unwrapped; all encrypted settings are unreadable.
- Wallet addresses and balance history (unencrypted) are unaffected.

**Recovery when SECRET_KEY is lost:**

```bash
# 1. Stop services
docker compose down

# 2. Re-generate a new master key
bash scripts/setup-secrets.sh   # generates new master_key.hex only if missing;
                                  # if master_key.hex was lost, delete it first:
rm secrets/master_key.hex && bash scripts/setup-secrets.sh

# 3. Clear the corrupted key_state row so init re-initialises
docker compose run --rm -e DATABASE_URL=postgresql+psycopg://audr:$(cat secrets/db_password.txt)@db:5432/audr api \
    python -c "
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with async_sessionmaker(engine)() as s:
        await s.execute(text(\"DELETE FROM key_state WHERE name = 'master_key'\"))
        await s.commit()
    await engine.dispose()
asyncio.run(main())
"

# 4. Re-enter provider credentials through the UI
docker compose up -d
```

**To avoid key loss:** back up `secrets/master_key.hex` to a secure offline location (password manager, encrypted USB). Rotate it periodically by generating a new value, re-wrapping the master key, and updating the environment.

---

## Restart persistence

All persistent data lives in the `db_data` Docker named volume. The volume survives `docker compose down` and `docker compose restart`. Data is lost only if the volume is explicitly removed:

```bash
docker compose down -v    # WARNING: destroys all data
docker volume rm audr_db_data
```

On restart, the `migrate` service re-runs Alembic migrations (idempotent) and then `audr.operations.init_key`, which validates that the SECRET_KEY can unwrap the stored master key, before `api` and `worker` start. (These were two services, `migrate` and `init`, until AUD-386 folded them into one — see [containers.md](containers.md).)

---

## Backups

audr has no proprietary indexer — a lost `db_data` volume with no backup means
every wallet, holding, and valuation is gone. `scripts/backup.sh` and
`scripts/restore.sh` (AUD-390) close that gap, encrypted from the first run.
See [security-at-rest.md](security-at-rest.md) for the threat model this sits
inside.

### Running a backup

```bash
./scripts/backup.sh                    # writes to ./backups
./scripts/backup.sh /mnt/offsite        # or a configurable destination
```

This pipes `pg_dump` (run inside the `db` container via `docker compose exec`)
straight through [`age`](https://github.com/FiloSottile/age) and writes
`audr-YYYYMMDD-HHMMSS.sql.age` — there is never an intermediate plaintext dump
on disk. If `age` is not installed, the script falls back to
`gpg --symmetric` (AES-256) and writes `.sql.gpg` instead.

**Zero-config first backup**: if no recipient key exists yet, the script
generates one on first run — an `age` identity at `secrets/backup_key.txt`
(mode `600`) — and prints its public key. You do not need to set up a key
before taking your first backup. To use a recipient key you manage elsewhere
(e.g. a hardware key, a key held outside this host) instead of the
auto-generated one, set `AUDR_BACKUP_RECIPIENT` to its `age1...` public key.

**`secrets/master_key.hex` is copied separately**, as `master_key-<ts>.hex`
next to the dump, and is deliberately **not** bundled into the encrypted
archive — the whole point of keeping the KEK outside the database is that
compromising one does not compromise the other. Back this file up with the
same discipline you'd apply to `secrets/master_key.hex` itself (see
[Key-loss behavior](#key-loss-behavior)). **A dump without it is not fully
recoverable**: the data restores fine, but every RPC URL and quote-provider
API key stored in `integration.encrypted_blob` stays permanently unreadable.

### Retention

There is no automated pruning. Treat `age`-encrypted dumps as cheap (a few
hours of data loss, at most) and keep a simple rotation — e.g. daily for a
week, weekly for a month — pruned by a cron job or your backup storage's own
lifecycle rules. Each `master_key-<ts>.hex` is identical as long as the key
hasn't been rotated, so only the most recent one needs to be kept offsite,
but it costs nothing to keep one per dump.

### Restoring

```bash
./scripts/restore.sh audr-20261002-131755.sql.age
# "already has N table(s) ... Refusing to restore" if the target DB is not empty
./scripts/restore.sh --force audr-20261002-131755.sql.age
```

`restore.sh` decrypts with `secrets/backup_key.txt` (or `--identity FILE`) and
restores into the `db` service via `docker compose exec`. It refuses to run
against a database that already has tables unless `--force` is passed, in
which case it drops and recreates the `public` schema first. If
`secrets/master_key.hex` was lost along with the volume, restore the matching
`master_key-<ts>.hex` to that path too — otherwise the restored
`key_state` row won't unwrap and provider credentials stay unreadable even
though the restore itself succeeds.

Before restoring into a database that already has tables (e.g. a Compose
project that was freshly recreated), make sure `migrate` has actually
finished applying Alembic migrations — `docker compose up -d --wait db migrate`
is **not** sufficient for this, since `migrate` has no healthcheck and
`--wait` is satisfied as soon as the container is *running*, not once it
*exits*. Use `docker compose run --rm migrate` (blocks until it exits) or
just bring up `api` too and wait for *that* to become healthy, since `api`
depends on `migrate`'s completion.

### Restore drill

A full backup → `docker compose down -v` (total volume loss) → restore → API
health → data-intact cycle was run once against a throwaway Compose stack for
AUD-390; see [verification-history.md](verification-history.md#verification-encrypted-backuprestore-drill-aud-390)
for the steps and a race condition found (and worked around) in the drill
itself.

---

## Exports

Portfolio and history export (JSON and CSV) is implemented. Both are available from the Account & Data page and directly:

```
GET /api/v1/exports/portfolio?format=json|csv
GET /api/v1/exports/history?format=json|csv&from=...&to=...
```

Both require an authenticated session. Exports are schema-versioned and contain no passwords, session tokens, RPC URLs, headers or API keys; unknown values are null or empty with an explicit status, never zero. See [api.md](api.md#exports-and-data-lifecycle) for the record schema.

---

## Provider data purge

To remove all RPC and quote provider credentials:

```bash
docker compose exec api python -c "
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
async def main():
    url = os.environ['DATABASE_URL']
    engine = create_async_engine(url)
    async with async_sessionmaker(engine)() as s:
        await s.execute(text('DELETE FROM integration'))
        await s.commit()
    await engine.dispose()
asyncio.run(main())
"
```

This does not affect wallet addresses or balance history.

---

## Reset command

To completely wipe all application data and start fresh:

```bash
# Stop and remove containers, networks, and volumes
docker compose down -v

# Remove secrets (optional — re-run setup-secrets.sh to generate new ones)
rm -f .env secrets/db_password.txt secrets/master_key.hex

# Re-initialise
bash scripts/setup-secrets.sh
docker compose up -d
```

---

## Deployment to remote host

```bash
# Deploy the latest images to the production host
./scripts/deploy.sh
```

The script:
1. Copies `compose.yaml` to the remote host over SCP
2. Pulls the latest images from the Harbor registry
3. Runs `alembic upgrade head` via the `migrate` service
4. Restarts all services with `docker compose up -d --remove-orphans`
5. Polls `/health` until healthy (60 s timeout)

**Remote host requirements:** Docker Engine 26+, SSH key in `./id_ed25519`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `migrate` exits with `SECRET_KEY is not set` | `.env` missing or not mounted | Run `bash scripts/setup-secrets.sh`, restart |
| `migrate` exits with `SECRET_KEY must be 32 bytes` | Corrupt `master_key.hex` | Regenerate per key-loss recovery above |
| `migrate` exits non-zero | DB not healthy or migration conflict | `docker compose logs migrate`, check DB logs |
| `api` health returns 503 | Migration not complete | Wait for `migrate` to finish; check `docker compose ps` |
| `worker` logs `no RPC integration configured` | RPC provider not set | Informational — the keyless public endpoints still work; configure your own RPC URL in Settings → Integrations to upgrade |
| `GET /` returns 200 but the UI shows no data | API is down; nginx served the SPA fallback | Check `curl -s http://localhost/health/ready` for a `"status":"ok"` body, never a bare `/` |
