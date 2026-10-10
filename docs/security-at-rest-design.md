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
  In today's schema that is exactly two: `wallet.label`, and `wallet.address`
  via a blind index — a keyed HMAC column carrying the uniqueness constraint
  and the lookup, alongside the ciphertext. (An earlier revision of this file
  also listed "manual-asset notes"; no such column exists — manual assets have
  no free-text field.)
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
| 4 | Timeboxed spike: `pg_tde` on Percona PG17 — image swap, keyring, upgrade path, rollback | B | M | infra |
| 5 | Password-derived KEK (true rotki parity) — **blocked on the product decision in §4** | — | L | founder |

Items 1 and 2 — both done — are what actually close the realistic threat (a
disk or a backup leaves the building), and neither touched the application or
the zero-config promise. 3a is additive. 3b is the one item here that changes
semantics rather than just storage, since the address uniqueness constraint has
to move to the blind index; it is a migration, not a column rewrite.

Two specifics about 3b that the 3a work surfaced, recorded here so they are not
rediscovered during implementation:

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

Note also that 3b widens the blast radius of a lost KEK: today losing it costs
provider credentials and labels, after 3b it costs the wallet list itself.

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
3. **Two-tier keys.** A machine key (as today) protects operational data the
   worker needs — quotes, the event index, news. A password-derived key
   protects the owner-identifying set — addresses, labels, holdings — which the
   worker arguably does not need in plaintext. Best balance, most work, and it
   needs a careful audit of what the worker actually reads.

Until that is decided, items 1–4 stand on their own and none of them foreclose
any of the three.
