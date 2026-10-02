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
[Browser] ─── HTTP :80 ──► [FastAPI :8000 / api]
                             │   /                  ──► SPA from /app/static
                             │   /api/*, /health/*  ──► API routes
                             ▼
                     [PostgreSQL :5432 / db]
                             ▲
                     [worker] ──── periodic jobs ────┘
```

The `api` container serves everything on one port: the compiled React SPA for
`/` and client-side routes, and the API for `/api/*` and `/health/*`. A separate
nginx `web` container used to do the static serving and reverse-proxy the API;
AUD-388 removed it — see `docs/containers.md` for the rationale and the
trade-off. The worker polls the job queue; it never binds a port.

---

## Loopback setup

All services communicate over the `internal` Docker bridge network. The only published port is `api`'s `80:8000`. This means:

- The database is never reachable from the host.
- The worker connects to the database using the Docker DNS name `db`.
- For local development against a compose stack, the API is at `http://localhost/api/`.

To reach the API inside the container, bypassing the published port:

```bash
docker compose exec api curl -s http://localhost:8000/health/live
```

---

## TLS / HTTPS proxy

The current configuration serves HTTP only. For production, place a reverse proxy (Caddy, Traefik, or nginx on the host) in front of the published port 80 and terminate TLS there. Doing so also recovers the static-serving qualities (gzip/brotli, cache headers) that were lost when the in-stack nginx container was removed in AUD-388.

**Caddy example** (`/etc/caddy/Caddyfile` on the host):

```
yourdomain.com {
    reverse_proxy localhost:80
}
```

Caddy handles certificate issuance and renewal automatically via Let's Encrypt.

Note on forwarded headers: no application code reads `X-Forwarded-Proto`,
`X-Forwarded-For` or `X-Real-IP`. The removed nginx container set them, but
nothing consumed them, so removing it changed no behaviour. If you put a
TLS-terminating proxy in front and need the app to know the external scheme or
the real client IP, that is a uvicorn concern, not application code — add
`--proxy-headers --forwarded-allow-ips=<proxy-ip>` to the `CMD` in the
`Dockerfile`. Uvicorn only trusts these headers from `127.0.0.1` by default, so
a proxy on another host or container IP is ignored until you widen that.

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
