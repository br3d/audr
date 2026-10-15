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
| `secrets/master_key.hex` | 64-char hex master key-encryption-key — the backup copy of `SECRET_KEY`, which is not recoverable from anywhere else |
| `.env` | `DB_PASSWORD` (generated here) and `SECRET_KEY` (copied from `master_key.hex`) — the only credentials Compose reads |

The PostgreSQL password exists only as `DB_PASSWORD` in `.env`. There is no
Docker secret for it and no separate password file: the `DATABASE_URL` of the
`api`, `worker` and `migrate` services embeds the password, so Compose needs it
in `.env` regardless. If an older install still has a `secrets/db_password.txt`,
nothing reads it, and `setup-secrets.sh` will reuse the password from it when
`.env` is missing.

**Never commit `.env` or `secrets/`.** Both are in `.gitignore`.

### Configuring an RPC endpoint

There is nothing to configure on the host: open **Connections** in the web
interface and paste your RPC URL there. It is encrypted at rest with the master
key and validated on save. Leaving it empty is a supported setup — the keyless
public endpoints below keep working.

Filling a fresh instance with demo data (`scripts/seed_dev.sh`, including the
`AUDR_SEED_RPC_URL` it reads) is a development-only workflow and is documented
in [development.md](development.md#demo-data).

### Keyless RPC fallback

Configuring an RPC integration is an upgrade, not a prerequisite. The chain
readers (`balance_scan`, `event_indexer`) always append the keyless public
endpoints listed in `backend/src/audr/providers/rpc_defaults.py` after whatever
is configured, and move on to the next endpoint whenever one reports itself
unusable. The failover rules are in
[architecture.md](architecture.md#rpc-failover); what matters when you operate
an instance:

- An exhausted Infura/Alchemy plan degrades to public endpoints instead of
  taking every chain-reading job down. A keyed provider is picked up again by
  itself on the next job run, once its quota resets.
- A rebound hostname is still a hard failure: if the stored URL stops passing
  SSRF validation the job fails rather than silently falling back.
- Validation on the Connections page does **not** fall back — it probes exactly
  the endpoint you configured, so the page keeps telling the truth about your
  own key.
- Public endpoints are shared infrastructure with their own unannounced rate
  limits. A sustained 402 on the configured provider is worth fixing, not
  living on.

### Keyless asset icon cache

Token logos are resolved the same keyless-by-default way as RPC and quotes:
the worker tries Trust Wallet's public GitHub asset repo first, then falls
back to CoinGecko's keyless contract-lookup endpoint, and caches whichever
image it finds in the `asset_icon` table. `GET /assets/{id}/icon` only ever
serves what is already cached — the frontend falls back to a generated
monogram for anything not yet resolved, so a cold cache never slows down the
dashboard.

Set `ASSET_ICONS_REMOTE_FETCH=false` in `.env` to stop the backend from ever
contacting GitHub or CoinGecko for icons; the UI then shows monograms only. The
request budget for the keyless CoinGecko fallback is tunable too — both
variables are in the
[configuration reference](architecture.md#8-configuration-reference).

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
`/` and client-side routes, and the API for `/api/*` and `/health/*`. There is
no separate web server in the stack — see [containers.md](containers.md) for why
each of the three services exists. The worker polls the job queue; it never
binds a port.

---

## Loopback setup

All services communicate over the `internal` Docker bridge network. The only published port is `api`'s `80:8000`. This means:

- The database is never reachable from the host.
- The worker connects to the database using the Docker DNS name `db`.
- For local development against a compose stack, the API is at `http://localhost/api/`.

To reach the API inside the container, bypassing the published port:

```bash
docker compose exec api python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health/live').read().decode())"
```

The image installs no OS packages at all, which keeps it fully digest-pinned,
so it ships no `curl`: use the interpreter for anything that needs an HTTP call
from inside a container. From the host,
`curl http://localhost/health/live` through the published port still works.

---

## TLS / HTTPS proxy

The stack serves HTTP only. For production, place a reverse proxy (Caddy, Traefik, or nginx on the host) in front of the published port 80 and terminate TLS there. That proxy is also where static-serving qualities belong — gzip/brotli compression and cache headers for the SPA assets, which the API process itself does not add.

**Caddy example** (`/etc/caddy/Caddyfile` on the host):

```
yourdomain.com {
    reverse_proxy localhost:80
}
```

Caddy handles certificate issuance and renewal automatically via Let's Encrypt.

Note on forwarded headers: no application code reads `X-Forwarded-Proto`,
`X-Forwarded-For` or `X-Real-IP`. If you put a TLS-terminating proxy in front
and need the app to know the external scheme or the real client IP, that is a
uvicorn concern, not application code — add
`--proxy-headers --forwarded-allow-ips=<proxy-ip>` to the `CMD` in the
`Dockerfile`. Uvicorn only trusts these headers from `127.0.0.1` by default, so
a proxy on another host or container IP is ignored until you widen that.

---

## Key-loss behavior

The application uses a two-layer encryption scheme:

1. **SECRET_KEY** (env var, set from `secrets/master_key.hex`): the key-encryption-key (KEK). Stored only outside the database.
2. **Master key**: a 32-byte AES-256 key generated on first boot, stored in the `key_state` table as a blob encrypted by the KEK.

The master key is used to encrypt provider credentials (RPC URLs, API keys) stored in the `settings` table, and, as of AUD-488, `wallet.label`, and, as of AUD-490, `wallet.address` (looked up via a separate deterministic HMAC blind index, `wallet.address_bidx`, derived from the master key but not reversible back to it).

This scheme covers credentials, wallet labels, and wallet addresses — holdings and the valuation history are still plaintext in Postgres. For the full at-rest picture, the threat model, and the recommended volume encryption for the `db_data` volume, see [security-at-rest.md](security-at-rest.md).

**If SECRET_KEY is lost (global, at startup):**

- The application will refuse to start (startup validation fails): `migrate`'s `init_key` step cannot unwrap `key_state.wrapped_key`, so it exits non-zero and `api`/`worker` never come up.
- The master key cannot be unwrapped; all encrypted settings, wallet labels, and wallet addresses are unreadable.
- **AUD-490 widened the blast radius here.** Before AUD-490 only labels and credentials were lost — the wallet list itself still rendered, with addresses and holdings intact, because `wallet.address` was plaintext. Since `wallet.address` is now behind the same master key, a lost `SECRET_KEY` makes the wallet list itself unreadable: there is no way to show, export, or scan balances for an address nobody can decrypt. Balance and valuation history rows (plaintext, keyed by `wallet_id`) are not deleted, but with no way to recover which row belongs to which address, they are effectively orphaned until the key is restored. This is why `secrets/master_key.hex` backup discipline (below) matters strictly more after AUD-490 than it did when only labels were at stake.

**If a single row's envelope is unreadable (per-row, at runtime):** this is a different failure from the one above — the key itself is fine (the app is running, so `init_key` already validated it at boot), but one wallet's `label_ciphertext` or `address_ciphertext` does not decrypt, e.g. a corrupted row, or a value somehow carrying another wallet's AAD (the label and address envelopes use distinct AAD prefixes — `wallet:label:` vs `wallet:address:` — so an envelope moved between the two columns fails to authenticate rather than silently decrypting as the wrong field). Provider credentials still fail loudly here (`GET /integrations` raises `InvalidEnvelopeError`, surfaced as a 500) — there is exactly one credential per kind, so there is nothing to degrade to.

Labels and addresses chose differently from each other. `GET /wallets` lists every tracked wallet in one response; a label is display-only (nothing filters, sorts, or keys off it), so letting one bad row fail the whole list is a worse outcome than it failing loudly would be for a credential — `audr.wallets.service.decrypt_label` catches the decrypt failure and returns the placeholder string `"[unreadable]"` for that wallet's label instead of raising, and the rest of the list, and every other field on that same wallet, are unaffected. `decrypt_address` makes the opposite choice: an address identifies the wallet, drives balance scanning, and has no sensible placeholder, so a corrupted `address_ciphertext` row raises and surfaces as a 500 for the whole list rather than silently showing `"[unreadable]"` next to a holdings table nobody could act on anyway. The master key itself failing to unwrap on a *read* also propagates now (there is no separate "degrade for the global case" path the way labels had one) — this should never reach a request handler in practice since `init_key` already fails the deploy first. Writing a label or address (`POST /wallets`, `PATCH /wallets/{id}`) always calls `get_master_key` directly and lets both errors propagate — neither can be *written* without a usable key, so creating or renaming a wallet still fails loudly if the key is gone. Setting a fresh label on an affected wallet clears its placeholder; there is no equivalent for a corrupted address row, since the address is the row's identity, not an editable field.

**Recovery when SECRET_KEY is lost:**

```bash
# 1. Stop services
docker compose down

# 2. Re-generate a new master key
bash scripts/setup-secrets.sh   # generates new master_key.hex only if missing;
                                  # if master_key.hex was lost, delete it first:
rm secrets/master_key.hex && bash scripts/setup-secrets.sh

# 3. Clear the corrupted key_state row so init re-initialises
docker compose run --rm -e DATABASE_URL="postgresql+psycopg://audr:$(sed -n 's/^DB_PASSWORD=//p' .env)@db:5432/audr" api \
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

On restart, the `migrate` service re-runs Alembic migrations (idempotent) and then `audr.operations.init_key`, which validates that the SECRET_KEY can unwrap the stored master key, before `api` and `worker` start. Why that is a separate one-shot service rather than API startup code is in [containers.md](containers.md).

---

## Encrypting the database volume

Holdings and the whole valuation history are plaintext at the column level
(wallet addresses and labels are not, as of AUD-488/AUD-490; `onchain_event`
no longer stores the tracked wallet's own address at all, as of AUD-492 —
only the third-party `counterparty_address`, see AUD-389 item 3c), so **the
volume is the layer that protects them**. Putting it on LUKS or a ZFS
native-encrypted dataset is audr's recommended at-rest baseline; what that
does and does not protect against is in
[security-at-rest.md](security-at-rest.md#2-what-to-do-about-it).

`compose.encrypted-volume.yaml` is the overlay that points `db_data` at a
directory on an encrypted mount. Two variables in the audr directory's `.env`:

```bash
COMPOSE_FILE=compose.yaml:compose.encrypted-volume.yaml
AUDR_DB_DATA_PATH=/mnt/encrypted/audr/db
```

Unlock and mount that filesystem **before** the stack starts — on an
unattended reboot that means a keyfile or TPM2 unlock ordered before Docker,
not an interactive passphrase. If `AUDR_DB_DATA_PATH` is unset, `docker
compose` refuses to start rather than falling back to the unencrypted default.

Point it at an **empty** directory. Postgres `initdb`s into it on first start
and chowns it to the container's `postgres` user — uid 70, which usually maps
to no host account, so `ls` on it as a normal host user returns *Permission
denied*. That is expected, not a failure.

### Decide before the first `docker compose up`

Adding the overlay to an install that already has a `db_data` volume is a
**silent no-op**. Docker applies `driver_opts` only when it creates a volume;
for a name that already exists it reuses the existing one and ignores the
options entirely — no error, no warning. Postgres keeps serving from the
unencrypted volume, the encrypted directory stays empty, and nothing in the
output says so.

One command tells the two apart:

```bash
docker volume inspect audr_db_data --format '{{.Options}}'
# map[device:/mnt/encrypted/audr/db o:bind type:none]   <- on the encrypted mount
# map[]                                                 <- NOT encrypted, overlay ignored
```

Empty `map[]` with the overlay in `COMPOSE_FILE` means the move never happened.
Migrating an existing install is a dump-and-restore, because the volume has to
be destroyed for Docker to recreate it with the bind:

```bash
# 1. Back up, and verify you can read the backup back. This is the only copy
#    of the data for the next few steps — see "Restoring" below.
./scripts/backup.sh

# 2. Destroy the unencrypted volume. Nothing after step 1 recovers it.
docker compose down -v

# 3. Add COMPOSE_FILE and AUDR_DB_DATA_PATH to .env as above, then start.
#    Confirm the bind took effect before restoring anything into it.
docker compose up -d
docker volume inspect audr_db_data --format '{{.Options}}'

# 4. Restore into the empty database. No --force: it has no tables yet.
./scripts/restore.sh backups/audr-<timestamp>.sql.gz.age
```

Keep `secrets/master_key.hex` exactly as it was across those steps — the
restored `key_state` row is wrapped with it, and a replaced key leaves provider
credentials unreadable.

The old volume's bytes remain recoverable on the unencrypted disk until that
region is overwritten; on an SSD, `docker volume rm` does not change that.
Where the plaintext history mattering is the point, the honest fix is to
encrypt the disk under Docker's data root as well, or to rebuild the host.

`secrets/master_key.hex` is unaffected by any of this — it lives on the host
filesystem, not in the volume, and volume encryption does nothing for it. See
[key-loss behavior](#key-loss-behavior).

---

## Scrubbing plaintext left by an encrypt-a-column migration

A migration that encrypts an existing column — 0021 (`wallet.label`), 0022
(`wallet.address`), and any future one — does **not** take the old plaintext
off disk on a stand that already had rows. It leaves it in two places:

- `ALTER TABLE ... DROP COLUMN` is a catalog-only operation. It marks the
  column dropped and stops returning it; the bytes stay in every existing
  tuple.
- The encrypting `UPDATE` supersedes each row rather than overwriting it, and
  the old row version keeps a complete plaintext copy.

This is not theoretical: the staging stand's `wallet` heap still contained
three plaintext wallet addresses after 0022 had run and the column was gone
from the schema. Measurements and the controlled experiment are in
[verification-history.md](verification-history.md#2-drop-column-does-not-take-the-plaintext-off-disk).

Fix is a table rewrite, which `VACUUM FULL` performs:

```bash
# After deploying a migration that encrypts a column on a stand with data.
# Brief ACCESS EXCLUSIVE lock — fine for `wallet` (a handful of rows); use
# pg_repack instead if the table is large enough for the lock to matter.
docker compose exec db psql -U audr -d audr -c "VACUUM (FULL, ANALYZE) wallet"
```

Verify, rather than assuming — the file path changes when the rewrite happens,
so check the *new* one:

```bash
docker compose exec db psql -U audr -d audr -At \
  -c "CHECKPOINT" -c "SELECT pg_relation_filepath('wallet')"
# base/16384/27720

docker compose exec db sh -c \
  'strings /var/lib/postgresql/data/base/16384/27720 | grep -coE "0x[0-9a-fA-F]{40}"'
# 0   <- clean. Non-zero means the rewrite did not happen or ran on the old path.
```

Two residues survive the rewrite and are not reachable from SQL:

- **Retained WAL** holds whatever was written until those segments recycle.
- **The unlinked heap file's blocks** stay on the raw device until something
  overwrites them; on an SSD this is not under the filesystem's control.

Both are exactly what the [volume-level baseline](#encrypting-the-database-volume)
is for. Column encryption protects a leaked dump or a Postgres-only compromise;
it has never been the layer that protects raw disk blocks.

Note this cannot be folded into the Alembic migration itself — `VACUUM FULL`
refuses to run inside a transaction block, and Alembic runs migrations in one.

---

## Backups

audr has no proprietary indexer — a lost `db_data` volume with no backup means
every wallet, holding, and valuation is gone. `scripts/backup.sh` and
`scripts/restore.sh` close that gap, encrypted from the first run. See
[security-at-rest.md](security-at-rest.md) for the threat model this sits
inside.

### Running a backup

```bash
./scripts/backup.sh                    # writes to ./backups
./scripts/backup.sh /mnt/offsite        # or a configurable destination
```

This pipes `pg_dump` (run inside the `db` container via `docker compose exec`)
through `gzip` and then [`age`](https://github.com/FiloSottile/age), writing
`audr-YYYYMMDD-HHMMSS.sql.gz.age` — there is never an intermediate plaintext
dump on disk. If `age` is not installed, the script falls back to
`gpg --symmetric` (AES-256) and writes `.sql.gz.gpg` instead.

**Compression comes before encryption**, and the order is not a style choice:
ciphertext does not compress, so a dump that was encrypted first can never be
shrunk afterwards. Measured on the stand's live database (AUD-564): 282 MiB
uncompressed, **49 MiB** compressed — 5.7x. `AUDR_BACKUP_COMPRESS=none` writes
the old uncompressed shape, for an operator who wants an archive that `age -d`
alone turns back into SQL.

`restore.sh` reads **both** shapes, selecting the decompression step from the
`.gz` in the filename, so dumps taken before this change restore with the
current script unchanged. That is proven by a drill rather than assumed — see
[verification-history.md](verification-history.md).

**Zero-config first backup**: if no recipient key exists yet, the script
generates one on first run — an `age` identity at `secrets/backup_key.txt`
(mode `600`) — and prints its public key. You do not need to set up a key
before taking your first backup. To use a recipient key you manage elsewhere
(e.g. a hardware key, a key held outside this host) instead of the
auto-generated one, set `AUDR_BACKUP_RECIPIENT` to its `age1...` public key.

**The KEK is copied separately**, as `master_key-<ts>.hex` next to the dump,
and is deliberately **not** bundled into the encrypted archive — the whole
point of keeping the KEK outside the database is that compromising one does not
compromise the other. Back this file up with the same discipline you'd apply to
`secrets/master_key.hex` itself (see
[Key-loss behavior](#key-loss-behavior)). **A dump without it is not fully
recoverable**: the data restores fine, but every RPC URL and quote-provider
API key stored in `integration.encrypted_blob` — and, since AUD-490, every
wallet address — stays permanently unreadable.

The KEK is read from `secrets/master_key.hex`, or, when that file does not
exist, from `SECRET_KEY` in `.env` (override the location with
`AUDR_ENV_FILE`). Both hold the same 64 hex characters;
`setup-secrets.sh` writes both, but **the deploy path renders only `.env`**, so
on the owner stand `secrets/master_key.hex` is absent by design. Before AUD-562
the script looked only at the file and so reported "provider credentials cannot
be recovered" on every backup taken there — a warning naming a path that was
never going to exist on that host, about a key that was in fact present.

Set `AUDR_BACKUP_SKIP_KEY_COPY=1` to suppress the copy and print where the KEK
actually lives instead. The scheduled on-host path sets it; see
[what the schedule does and does not protect against](#what-the-schedule-does-and-does-not-protect-against)
for why a KEK file beside the dump it decrypts is worth less than nothing.

### The scheduled backup on the owner stand (AUD-562)

The owner stand takes one encrypted dump a day from a systemd timer. Install or
update it from a clone:

```bash
./scripts/install-backup.sh            # install/update, take a first dump, show status
./scripts/install-backup.sh --check    # report what is installed; change nothing
```

That installs `audr-backup.timer` → `audr-backup.service` →
`scripts/backup-run.sh`, which per run takes a dump, **decrypts its own output**
and checks it starts like a `pg_dump`, prunes to retention, and alerts through
[`notify.sh`](deploy-runbook.md#alerting-how-you-find-out-the-stand-is-down)
if any of that failed. A run that fails leaves a flag
and the next success stands the alert down, so a backup that silently stops
being taken is itself an alertable condition — that was the actual AUD-562 gap,
not missing tooling.

As with the watchdog, the scripts on the host are **pinned copies** under
`~/.local/share/audr-backup/`, not a git checkout: the only checkout on that
host belongs to the CI runner, which rewrites it on every deploy. Changing
`backup.sh`, `backup-run.sh` or `dump-prune.py` in this repo does not reach the
host until `install-backup.sh` runs again.

#### What the schedule does and does not protect against

**Covered**: loss of the `db_data` volume, a bad migration, an accidental
`docker compose down -v`, a table dropped by hand — the cases that are noticed
within hours or days.

**Not covered: loss of the host.** The dumps sit on the same machine as the
database they came from, and the KEK that makes their wallet addresses and
provider credentials readable (`.env`'s `SECRET_KEY`) also exists only on that
machine. Fire, theft, a dead disk or a wiped host loses both halves at once.

That is a deliberate scope, not an oversight. The scheduled path explicitly
does **not** write a KEK copy next to the dumps (`AUDR_BACKUP_SKIP_KEY_COPY=1`):
AUD-426 removed a plaintext `SECRET_KEY` duplicate from this very directory,
and a key file beside the archive it decrypts is that same duplicate with the
encryption cancelled out — it hands both halves to anyone who can read the host.

**Closing the host-loss gap is an owner action** and cannot be automated from
the stand without putting an off-host credential on the stand. It means copying
two things somewhere else, *stored apart from each other*:

1. the newest `~/audr/backups/audr-*.sql.gz.age`, and
2. the `SECRET_KEY` value from `~/audr/.env` (equivalently
   `secrets/master_key.hex` — the same 64 hex characters), plus
   `secrets/backup_key.txt`, the `age` identity that decrypts the dump itself.

Without (2) a restore from (1) succeeds and still cannot show a single wallet
address — see [Key-loss behavior](#key-loss-behavior) and AUD-490.

#### Retention

`scripts/dump-prune.py` applies one GFS rule to both dump families we own
(Paperclip's archive and audr's own) through `--prefix`, rather than there
being a second pruner to keep in step. On the stand the scheduled path runs:

```bash
python3 dump-prune.py --dir ~/audr/backups --prefix audr --days 7 --weeks 4 --apply
```

A dump survives if it is from the last 7 days, **or** is the newest dump of one
of the 4 most recent older ISO weeks. The newest dump in the directory is
protected unconditionally, so neither a clock skew nor a stalled timer can
empty it, and files that are not dumps — including any `master_key-<ts>.hex`
left by a manual backup — are never candidates.

Sizing, measured rather than guessed: one dump of the live database is **49
MiB** since AUD-564 compressed it (282 MiB before — `age` does not compress,
and neither does plain-text `pg_dump`), so steady state is 11 files, **~0.6
GiB**, on a root filesystem with ~11 GiB free. The pre-AUD-564 dumps still in
the directory are the big ones and age out of it on the same rule.

The `--weeks` cap is the part that must not be dropped. Compression bought time
here, not an exemption: without the cap the weekly tier still grows forever,
now at ~2.5 GiB/year instead of ~14.7. A host that runs out of space because of
its own backups is a worse outage than the one they prevent, and it arrives
silently. The cap is affordable because these dumps are a *recovery point, not
an archive*: the live database holds the full valuation history, and restoring
a two-month-old snapshot of a portfolio tracker is close to useless anyway.

The same arithmetic is why `--days 7 --weeks 4` was **not** widened in the same
change that made the dumps small. Retention is a recovery-window decision; this
change only made the window cheaper to hold.

For a manual dump taken to carry off the host, leave `AUDR_BACKUP_SKIP_KEY_COPY`
unset — there you *want* the `master_key-<ts>.hex` beside it, because the two
are about to be separated from the host rather than left on it. Each copy is
identical as long as the KEK has not been rotated.

### Searching a dump for a particular value

A dump produced by `scripts/backup.sh` is encrypted, so a plain `grep` over it
finds nothing regardless of what it contains. Decrypt on the fly rather than
writing a plaintext copy to disk:

```bash
age -d -i secrets/backup_key.txt backups/audr-20261015-172140.sql.gz.age \
  | gzip -dc | grep -c 'SOME_VALUE'
```

The same caution applies to the live database: `grep` over the PostgreSQL data
directory is not a reliable answer either, because Postgres stores wide
`text`/`jsonb` values out-of-line and compressed. Query through the database
engine (`docker compose exec db psql -U audr audr`), which decompresses
transparently. The measurements behind this are in
[verification-history.md](verification-history.md#why-grep-over-postgres-files-and-dumps-is-not-evidence).

### Restoring

```bash
./scripts/restore.sh audr-20261015-172140.sql.gz.age
# "already has N table(s) ... Refusing to restore" if the target DB is not empty
./scripts/restore.sh --force audr-20261015-172140.sql.gz.age
# pre-AUD-564 uncompressed dumps take the same command
./scripts/restore.sh --force audr-20261015-142732.sql.age
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

Two drills are on record, and the distinction between them is the point:

- **AUD-390** proved the *scripts*. A full backup → `docker compose down -v`
  (total volume loss) → restore → API health → data-intact cycle against a
  throwaway Compose stack, on synthetic data created through the real API. See
  [verification-history.md](verification-history.md#verification-encrypted-backuprestore-drill-aud-390).
- **AUD-562** proved an *artefact*. The actual dump the timer wrote on the
  owner stand was restored into an isolated throwaway stack and checked row by
  row against the live database, then decrypted with the live host's KEK. See
  [verification-history.md](verification-history.md#verification-scheduled-backup-and-a-live-artefact-restore-drill-aud-562).

Working scripts and a restorable backup are not the same claim, and only the
second one is what you have at 3am. Either write-up doubles as a template for
rehearsing a restore on your own instance; prefer the AUD-562 shape, because
"the file currently on disk restores" is the property worth re-checking.

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

# Remove credentials (optional — re-run setup-secrets.sh to generate new ones)
rm -f .env secrets/master_key.hex

# Re-initialise
bash scripts/setup-secrets.sh
docker compose up -d
```

---

## Running on a remote host

Nothing in the install is local-only: the same `scripts/setup-secrets.sh` plus
`docker compose up -d` is the procedure on a remote box, run over SSH in its
checkout. Upgrade it the same way as any other instance —
`git pull && docker compose pull && docker compose up -d`.

Put a TLS-terminating proxy in front of it before exposing port 80 beyond
localhost (see [TLS / HTTPS proxy](#tls--https-proxy)), and keep
`secrets/master_key.hex` and the backups off that host.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `migrate` exits with `SECRET_KEY is not set` | `.env` missing or not mounted | Run `bash scripts/setup-secrets.sh`, restart |
| `migrate` exits with `SECRET_KEY must be 32 bytes` | Corrupt `master_key.hex` | Regenerate per key-loss recovery above |
| `migrate` exits non-zero | DB not healthy or migration conflict | `docker compose logs migrate`, check DB logs |
| `api` health returns 503 | Migration not complete | Wait for `migrate` to finish; check `docker compose ps` |
| `worker` logs `no RPC integration configured` | RPC provider not set | Informational — the keyless public endpoints still work; configure your own RPC URL on the Connections page to upgrade |
| `GET /` returns 200 but the UI shows no data | The process is up and served the SPA, but the database behind it is not ready | Check `curl -s http://localhost/health/ready` for a `"status":"ok"` body, never a bare `/` |
