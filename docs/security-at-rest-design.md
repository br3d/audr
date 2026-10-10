# At-rest encryption: the option space and the open decision

Maintainer-facing companion to [security-at-rest.md](security-at-rest.md),
which states what audr encrypts today and what an operator should do about it.
This file records *why* it is that and not something else, so the comparison
does not have to be redone from scratch. Originally written up under AUD-389.

## 1. What rotki does, and why it does not port over

rotki (our reference implementation for self-hosted portfolio tracking) uses
SQLCipher on a per-user SQLite file. The DB key is derived from the **user's
login password**; the file on disk is a single opaque blob, and rotki's
background tasks only run while the user is unlocked. Their global DB (asset
metadata, historical prices) is a *separate, unencrypted* SQLite file.

Two things make that model work for rotki and not for us:

1. **One file, one key.** SQLCipher encrypts pages of a single file. Postgres
   has a cluster of files, WAL, temp files, and a shared buffer cache; there is
   no single-file equivalent.
2. **No unattended work.** rotki is a desktop app that does nothing while
   locked. audr runs a `worker` container that polls quotes, indexes on-chain
   events and refreshes news **24/7 with no user present**. A key that only
   exists after a human types a password cannot drive that worker.

The second point is the product decision in §4.

## 2. The option space for PostgreSQL

Community PostgreSQL **has no TDE** and, after ten years of mailing-list
discussion, still has none; every TDE implementation lives in a fork or an
extension. Ranked by fit for a self-hosted single-node deployment:

### A. Volume / filesystem encryption — LUKS or ZFS native (recommended baseline)

Encrypt the block device or dataset that backs the `db_data` Docker volume.
Protects against a disk leaving the building; protects against nothing while
the machine runs. Host setup only, no application or schema change. **This is
the adopted baseline** and is the recommendation carried in
[security-at-rest.md](security-at-rest.md#2-what-to-do-about-it).

### B. `pg_tde` (Percona) — real TDE as an open-source extension

Percona's `pg_tde` is open source, GA, and ships in Percona Distribution for
PostgreSQL 17; 2.2.x (2026) added AES-256 and production WAL encryption, with
keyring backends ranging from a local keyfile to KMIP/Vault.

- **Protects against:** the same disk-theft threat as (A), plus leaked data
  files or base backups copied off a *running* host, with per-database keys.
- **Cost:** swapping `postgres:16-alpine` for the Percona PG17 image — a
  different base, a major-version upgrade, and a vendor we do not control, on a
  product whose whole pitch is "docker compose up and it works". Keyring config
  becomes a new thing the self-hoster must get right, and a wrong keyring is an
  unrecoverable database rather than a few unreadable credential rows.
- **Verdict:** worth a timeboxed spike, not worth adopting blind. Revisit if a
  user asks for compliance-grade at-rest encryption.

#### Spike results (item 4, 2026-10-10)

The spike was run against `percona/percona-distribution-postgresql:17`
(Percona Server for PostgreSQL 17.11.1, `pg_tde` 2.2 in the image). Full
procedure, commands and raw output:
[verification-history.md](verification-history.md#verification-pg_tde-spike-aud-389-item-4).
What it settled:

- **It works, and the application does not have to know.** With
  `ALTER DATABASE audr SET default_table_access_method = tde_heap`, every table
  a migration creates afterwards is encrypted with no change to the models, the
  migrations or any query — unlike option (D), there is no "which columns can we
  encrypt" question, because indexes, aggregates and ordering all keep working.
  `ALTER TABLE ... SET ACCESS METHOD heap` converts a table back, so the
  decision is reversible per table rather than one-way.
- **The migration path is our existing backup tooling.** A plain `pg_dump` from
  community PG16 restores into a TDE-enabled PG17 database and the restored
  tables come out `tde_heap`. No `pg_upgrade` across two differently-built
  distributions is needed, which was the main feared risk of the image swap.
- **WAL encryption needs a two-phase first boot.** `pg_tde.wal_encrypt=on` on a
  fresh cluster is a `FATAL: principal key not configured` boot loop: the server
  key is set through a SQL function, which needs a server that will not start.
  First boot has to run with WAL encryption off, set the server key, then
  restart with it on. A self-hoster hitting that on install sees a database that
  will not start and no obvious way back — this is exactly the "new thing the
  self-hoster must get right" cost above, now measured rather than guessed.
- **Cost is ~9% on writes, nil on reads.** `pgbench -s 10`, 4 clients: 1084 →
  991 tps on the default write mix (-8.6%), and no measurable difference
  read-only (13.5k vs 13.7k tps, within noise on a working set that fits in
  shared buffers). Both numbers already include WAL encryption, so the delta is
  the `tde_heap` page cost alone.
- **The keyring reintroduces the same problem it is meant to solve.** With the
  local-keyfile provider, the 292-byte key sits on the same host as the data, so
  against whole-machine theft it adds nothing over option (A); it only helps
  when data files or a base backup leak off a running host. Pointing it at
  KMIP/Vault fixes that and hands the self-hoster a second service to run.
  Losing the keyfile is survivable-looking and worse than it looks: the server
  boots fine and every encrypted table fails at read time with
  `key "..." not found in key provider`.

**Conclusion: not adopting it now, and the reason is sharper than before the
spike.** What pg_tde buys over the adopted baseline is confidentiality of data
files leaked off a *running* host — and only if the keyring lives somewhere else,
which means Vault. The cost is a vendor-controlled image, a boot-order trap on
install, and a second key whose loss destroys the whole database rather than a
few columns. Items 1, 2 and the (D) column work cover the realistic threats
without any of that. Revisit if a user asks for compliance-grade at-rest
encryption, or if we ever run a KMIP/Vault service for other reasons — the
technical path is now known and the spike does not have to be redone.

### C. `pgcrypto` column encryption

Encrypt columns in SQL with `pgp_sym_encrypt`.

- **Verdict: no.** The key has to travel in the SQL statement, which puts it in
  `pg_stat_activity` and potentially the server log. Strictly worse than the
  envelope encryption already in the application, which keeps the key in the
  API process and never sends it to the database.

### D. Extend the existing application-level envelope encryption

Keep `crypto.py`, widen its coverage from credentials to portfolio data.

- **Protects against:** a Postgres-only compromise — a leaked dump, a read
  replica, a DB-level backup, or anyone who gets the volume but not the
  `secrets/` directory. The one layer that survives "attacker has the whole
  database but not the host's env".
- **Cost — the real constraint:** encrypted columns cannot be filtered,
  joined, ordered or aggregated in SQL. `portfolio/history_query.py` and the
  valuation rollups do exactly that, so blanket encryption would force those
  aggregations into Python. Workable fields are the ones only ever read whole.
  In today's schema that is `wallet.label`, and `wallet.address`
  via a blind index — a keyed HMAC column carrying the uniqueness constraint
  and the lookup, alongside the ciphertext. (An earlier revision of this file
  also listed "manual-asset notes"; no such column exists — manual assets have
  no free-text field.) `onchain_event`'s address columns belong to this set too
  and were missed; they are item 3c, and the fix there is to stop storing the
  owner's side rather than to encrypt it.
- **Verdict:** a good incremental second layer, scoped to non-aggregated
  columns. Do not attempt to encrypt the valuation/quote history this way.

### E. Encrypted backups — done

Whatever is picked above, the moment someone writes a dump it is a plaintext
copy of everything, outside whatever protection the volume had. `pg_dump` piped
through `age`/`gpg`, with the recipient key held to the same discipline as
`master_key.hex`, is implemented in `scripts/backup.sh` / `scripts/restore.sh`
(AUD-390) and drill-verified in
[verification-history.md](verification-history.md#verification-encrypted-backuprestore-drill-aud-390).

## 3. Where that leaves the backlog

| # | Action | Option | Effort | Owner |
| --- | --- | --- | --- | --- |
| 1 | ~~Document and recommend LUKS/ZFS for the `db_data` volume; make it part of first-time setup guidance~~ — **done**: `compose.encrypted-volume.yaml` overlay + [procedure](operations.md#encrypting-the-database-volume), linked from the README install step | A | S | infra |
| 2 | ~~Define a backup procedure, with `pg_dump` output encrypted by default~~ — **done** | E | S | infra |
| 3a | ~~Extend envelope encryption to `wallet.label` — the only free-text owner-written column that exists today, and the one that establishes the encrypted-column pattern (migration, model, round-trip tests)~~ — **done** (AUD-488): `wallet.label_ciphertext`, migration 0021, key-loss behaviour documented in [operations.md](operations.md#key-loss-behavior) | D | S | backend |
| 3b | ~~Encrypt `wallet.address`, replacing its `unique=True` with a unique HMAC blind-index column for lookup~~ — **done** (AUD-490): `wallet.address_ciphertext` + `wallet.address_bidx` (HKDF-derived subkey, not the master key itself), migration 0022. Of `operations/exports.py`'s three `ORDER BY w.address` queries, the bounded current-portfolio export now decrypts then sorts in Python (cheap at wallet×asset scale, keeps the old alphabetical-by-address order); the two streamed full-history queries order by `w.id` instead, since buffering the whole export just to sort on plaintext would defeat streaming. Key-loss behaviour documented in [operations.md](operations.md#key-loss-behavior) | D | M | backend |
| 4 | ~~Timeboxed spike: `pg_tde` on Percona PG17 — image swap, keyring, upgrade path, rollback~~ — **done** (2026-10-10): works, `default_table_access_method = tde_heap` needs no application change, dump/restore is the migration path, ~9% write cost; **not adopted** — see [spike results](#spike-results-item-4-2026-10-10) for why and for what would change the answer | B | M | infra |
| 3c | Stop storing the owner's own address in plaintext in `onchain_event` — `from_address`/`to_address` are plain `text`, and a row only exists when one of them *is* the tracked wallet, so every indexed event carries the owner's address in the clear. Cheapest shape: store the counterparty only, since `event_type` + `wallet_id` already determines which side the owner was on | D | M | backend |
| 3d | ~~Operator step after any encrypt-a-column migration: rewrite the table so the dropped plaintext actually leaves the heap~~ — **done**: [procedure](operations.md#scrubbing-plaintext-left-by-an-encrypt-a-column-migration), and applied to the staging stand's `wallet` table, which still held three plaintext addresses after 0022 | — | S | infra |
| 5 | Password-derived KEK (true rotki parity) — **blocked on the product decision in §4** | — | L | founder |

Items 1–4 are closed. 3c is open and is the one remaining place where an
owner-identifying value sits on disk in plaintext; item 5 is the founder's
decision in §4, not an engineering task.

**Correction (2026-10-10).** An earlier revision of this file called 3b "the
last owner-identifying column still stored as plaintext", and migration 0022's
docstring says the same. Both are wrong: `onchain_event.from_address` /
`to_address` were never in scope and still hold the owner's address on every
stand with event history — hence 3c. 3b took the address out of one table, not
off the disk. Separately, dropping a plaintext column does not remove its bytes
from the heap, so 3a/3b left recoverable plaintext behind on an already-deployed
stand until the table was rewritten — measured and fixed, hence 3d. Evidence for
both: [verification-history.md](verification-history.md#verification-what-the-worker-actually-reads-and-what-an-encrypt-a-column-migration-leaves-on-disk-aud-389-item-5-prep).

Items 1 and 2 are what actually close the realistic threat (a disk or a backup
leaves the building), and neither touched the application or the zero-config
promise. 3a was additive. 3b was the one item here that changed semantics rather
than just storage, since the address uniqueness constraint had to move to the
blind index; it was a migration, not a column rewrite.

Two specifics about 3b that the 3a work surfaced, kept here because they are the
reasoning behind the shipped shape and apply to any future encrypted column:

- **The constraint, not the column, is the hard part.** `add_wallet` detects a
  duplicate by catching the `IntegrityError` from `address`'s unique index. The
  envelope ciphertext cannot carry that index — AES-GCM uses a fresh nonce per
  write, so the same address encrypts to a different value every time. The
  uniqueness has to live on a deterministic keyed HMAC column, which should use
  a subkey derived from the master key rather than the master key itself, so a
  blind-index value can never be confused with encryption key material.
- **`operations/exports.py` is the one place SQL actually breaks.** Three raw
  queries `ORDER BY w.address`. Ordering by ciphertext is not a stable sort at
  all, so that ordering has to move — into Python after decryption, or onto
  another stable key. Every other reader (`jobs/__main__.py`,
  `jobs/event_indexer.py`, `portfolio/snapshot.py`, `portfolio/balances.py`,
  `api/wallets.py`, `api/holdings.py`) reads the address whole and needs nothing
  beyond the transient-attribute pattern 3a established.

Note also that 3b widened the blast radius of a lost KEK: it used to cost
provider credentials and labels, it now costs the wallet list itself.

## 4. The open product decision

Today `SECRET_KEY` sits in a file on the same host as the database. Anyone who
takes the whole machine takes both halves, and volume encryption only helps
while the machine is powered off. The rotki property — *"the data is useless
without the password only the owner knows"* — requires deriving the KEK from
the owner's login password (Argon2id) and holding the master key in memory only
for the duration of a session.

That directly conflicts with the always-on `worker`. The three ways out:

1. **Keep the current model.** `SECRET_KEY` on disk, background jobs always
   run. Simplest; disk theft of a *powered-off* box is covered by LUKS.
2. **Password-derived key, degraded background work.** Jobs run only while a
   session is live; quotes and the event index go stale when nobody logs in.
   Maximum confidentiality, visibly worse product.
3. **Two-tier keys — ruled out by measurement, 2026-10-10.** The idea was a
   machine key for what the worker needs (quotes, the event index, news) and a
   password-derived key for the owner-identifying set (addresses, labels),
   which the worker was assumed not to need in plaintext. That assumption was
   the one thing here nobody had checked, and it is false. The
   [worker audit](verification-history.md#1-what-the-worker-reads) walked the
   call graph of all eight job kinds: `BALANCE_SCAN`, `DISCOVERY` and
   `EVENT_INDEXER` each need the plaintext address because the address *is* the
   query — the RPC parameter for a balance read, the scan target for discovery,
   the padded log topic for the event filter. There is no ciphertext
   substitution; the chain only answers to the address. So a password tier over
   the owner-identifying set behaves exactly like option 2 (background work
   stops without a live session) for everything except `wallet.label`, which no
   job reads and which is display-only. A second key protecting only a cosmetic
   field is not worth its complexity.

**The real choice is therefore between 1 and 2**, and it is the same trade in
both directions: always-on background refresh, or data that is unreadable
without the owner present. Option 3 was the "have both" answer and it does not
exist at this architecture.

Items 1–4 shipped without it and none of them foreclose any of the three: the
envelope layer 3a/3b extended is exactly where a password-derived KEK would be
swapped in, and the pg_tde spike's conclusion is independent of which key model
wins. (The earlier note that option 3 would make the pg_tde question more
interesting no longer applies — splitting the schema by who needs plaintext was
the appealing part of option 3, and that split is what the worker audit
disallows.)
