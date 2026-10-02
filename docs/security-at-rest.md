# Encryption at rest (AUD-389)

> **Question this document answers:** SQLite has SQLCipher. audr runs on
> PostgreSQL. What is our equivalent, and what are we actually protecting
> against?

## 1. Where we stand today

audr already has **application-level envelope encryption**, built in the
original security work and documented in [operations.md](operations.md#key-loss-behavior):

| Layer | What it is | Where the key lives |
| --- | --- | --- |
| KEK | 64-hex `SECRET_KEY`, from `secrets/master_key.hex` | Host filesystem / env var, **outside** the DB |
| Master key | Random 32-byte AES-256 key, wrapped by the KEK | `key_state.wrapped_key` in Postgres |
| Payloads | AES-256-GCM, versioned envelope `version(1) || nonce(12) || ct+tag` | `backend/src/audr/operations/crypto.py` |

What is actually encrypted with it: **provider credentials only** — RPC URLs,
quote-provider API keys, the `allow_private_host` flag (`integration.encrypted_blob`,
see `backend/src/audr/settings/integrations.py`).

What is **plaintext on disk** right now:

- wallet addresses and labels (`wallet`)
- holdings, balances, valuation snapshots and lines (`valuation_snapshot`,
  `valuation_line`)
- the full price/quote history (`quote_set`, `quote_observation`)
- the asset catalog, on-chain event index, news cache
- the owner's Argon2/bcrypt password hash and session rows (hashes, by design)

For a portfolio tracker, the plaintext set *is* the sensitive set: net worth,
every address you own, and the link between them. So the gap the founder is
pointing at is real.

## 2. What rotki actually does, and why it does not port over

rotki (our reference implementation for self-hosted portfolio tracking) uses
SQLCipher on a per-user SQLite file. The DB key is derived from the **user's login
password**; the file on disk is a single opaque blob, and rotki's background
tasks only run while the user is unlocked. Their global DB (asset metadata,
historical prices) is a *separate, unencrypted* SQLite file.

Two things make that model work for rotki and not for us:

1. **One file, one key.** SQLCipher encrypts pages of a single file. Postgres
   has a cluster of files, WAL, temp files, and a shared buffer cache; there is
   no single-file equivalent.
2. **No unattended work.** rotki is a desktop app that does nothing while
   locked. audr runs a `worker` container that polls quotes, indexes on-chain
   events, and refreshes news **24/7 with no user present**. A key that only
   exists after a human types a password cannot drive that worker.

That second point is the actual product decision hiding in this ticket, and it
is the founder's call — see §5.

## 3. The option space for PostgreSQL

Community PostgreSQL **has no TDE** and, after ten years of mailing-list
discussion, still has none; every TDE implementation lives in a fork or an
extension. Ranked by fit for a self-hosted single-node deployment:

### A. Volume / filesystem encryption — LUKS or ZFS native (recommended baseline)

Encrypt the block device or dataset that backs the `db_data` Docker volume.

- **Protects against:** stolen disk, stolen laptop/NAS, a decommissioned drive,
  a cloud snapshot or backup tarball leaving the host.
- **Does not protect against:** anything while the machine is running — root,
  the Docker daemon, a compromised app, or `docker exec … psql`.
- **Cost:** host setup only, zero application change, zero schema change.
  AES-NI makes the throughput cost a few percent. Needs a passphrase at boot,
  or a keyfile/TPM2 unlock if the box must reboot unattended.
- **Verdict:** this is the honest answer to "what is our SQLCipher". It is
  what the Postgres community itself recommends, and it covers the single most
  likely real-world threat for a self-hosted box.

### B. `pg_tde` (Percona) — real TDE as an open-source extension

Percona's `pg_tde` is open source, GA, and ships in Percona Distribution for
PostgreSQL 17; 2.2.x (2026) added AES-256 and production WAL encryption, with
keyring backends ranging from a local keyfile to KMIP/Vault.

- **Protects against:** same disk-theft threat as (A), plus leaked data files
  or base backups copied off a *running* host, with per-database keys.
- **Cost:** we must swap `postgres:16-alpine` for the Percona PG17 image —
  a different base, a major-version upgrade, and a vendor we do not control, on
  a product whose whole pitch is "docker compose up and it works". Keyring
  config becomes a new thing the self-hoster must get right, and a wrong
  keyring is an unrecoverable database rather than a few unreadable credential
  rows.
- **Verdict:** worth a timeboxed spike, not worth adopting blind. Revisit if a
  user ever asks for compliance-grade at-rest encryption.

### C. `pgcrypto` column encryption

Encrypt columns in SQL with `pgp_sym_encrypt`.

- **Verdict: no.** The key has to travel in the SQL statement, which puts it in
  `pg_stat_activity` and potentially the server log. It is strictly worse than
  the envelope encryption we already have in the application, which keeps the
  key in the API process and never sends it to the database.

### D. Extend our existing application-level envelope encryption

Keep `crypto.py`, widen its coverage from credentials to the portfolio data.

- **Protects against:** a Postgres-only compromise — a leaked dump, a read
  replica, a DB-level backup, or anyone who gets the volume but not the
  `secrets/` directory. This is the one layer that survives "attacker has the
  whole database but not the host's env".
- **Cost — the real constraint:** encrypted columns cannot be filtered,
  joined, ordered, or aggregated in SQL. `portfolio/history_query.py` and the
  valuation rollups do exactly that today, so blanket encryption would force
  those aggregations into Python. Workable fields are the ones we only ever
  read whole: wallet labels, manual-asset notes, and (via a blind index — a
  keyed HMAC column for lookup alongside the ciphertext) wallet addresses.
- **Verdict:** a good incremental second layer, scoped to non-aggregated
  columns. Do not attempt to encrypt the valuation/quote history this way.

### E. Encrypted backups (separate — and we have no backup path at all)

Worth stating plainly: there is currently **no documented backup procedure**
in `docs/operations.md` and no `pg_dump` anywhere in `scripts/`. Whatever we
pick above, the moment someone writes a dump it is a plaintext copy of
everything, sitting outside whatever protection the volume had. The backup
path should be defined *and* encrypted in one go — `pg_dump` piped through
`age` or `gpg`, with the recipient key held to the same discipline as
`master_key.hex`.

**Update (AUD-390, done):** `scripts/backup.sh` / `scripts/restore.sh` now
implement exactly this — `pg_dump` piped through `age` (or `gpg --symmetric`
if `age` is unavailable), never touching disk unencrypted, with a
zero-config recipient key generated on first run and `master_key.hex`
captured in a separate, un-bundled file. See
[operations.md#backups](operations.md#backups) for the operator procedure and
[verification-history.md](verification-history.md#verification-encrypted-backuprestore-drill-aud-390)
for the executed restore drill.

## 4. Recommendation

| # | Action | Layer | Effort | Owner |
| --- | --- | --- | --- | --- |
| 1 | Document and recommend LUKS/ZFS for the `db_data` volume; make it part of first-time setup guidance | A | S | infra |
| 2 | ~~Define a backup procedure at all, with `pg_dump` output encrypted by default~~ — **done, AUD-390** | E | S | infra |
| 3 | Extend envelope encryption to wallet labels/addresses + manual-asset notes, with an HMAC blind index for address lookup | D | M | backend |
| 4 | Timeboxed spike: `pg_tde` on Percona PG17 — image swap, keyring, upgrade path, rollback | B | M | infra |
| 5 | Password-derived KEK (true rotki parity) — **blocked on a product decision**, see §5 | — | L | founder |

Items 1–3 are additive, carry no migration risk, and together close the
realistic threat (disk or backup leaves the building) without touching the
zero-config promise.

## 5. The open product decision

Today `SECRET_KEY` sits in a file on the same host as the database. Anyone who
takes the whole machine takes both halves, and volume encryption only helps
while the machine is powered off. The rotki property — *"the data is useless
without the password only the owner knows"* — requires deriving the KEK from
the owner's login password (Argon2id) and holding the master key in memory
only for the duration of a session.

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

## References

- [PostgreSQL wiki — Transparent Data Encryption](https://wiki.postgresql.org/wiki/Transparent_Data_Encryption)
- [pgEdge — Why Postgres lacks TDE](https://www.pgedge.com/blog/why-postgres-lacks-transparent-data-encryption)
- [Percona — pg_tde documentation](https://docs.percona.com/pg-tde/)
- [Percona — pg_tde is ready for production](https://www.percona.com/blog/the-pg_tde-extension-is-now-ready-for-production/)
