# Verification: US3 History (AUD-77–84, AUD-87–88)

**Date**: 2026-09-28  
**Branch**: feat/us3-history  
**Author**: Backend Developer

---

## Scope

This document records the verification steps and results for the US3 history
feature set: migration 006, history materialisation, reorg/canonicality
handling, history query, and the `GET /history` / `GET /history/{snapshot_id}`
API endpoints.

---

## Migration 006

### What was added

| Object | Purpose |
|--------|---------|
| `valuation_line.observation_id` | FK to `balance_observation` for exact line provenance |
| `balance_observation_invalidation` | Append-only table: marks observations as invalidated (reorg / verification_pending) |
| `history_point` | One materialized summary row per published snapshot (total_value_usd, quality, gap/canonical flags) |

### How to verify

```bash
# Apply to test database
TEST_DATABASE_URL=postgresql+psycopg://audr:audr@localhost:5433/audr_test \
  alembic -c backend/alembic.ini upgrade head

# Confirm all six revisions applied
psql postgresql://audr:audr@localhost:5433/audr_test \
  -c "SELECT version_num FROM alembic_version;"
# Expected: 006

# Confirm new tables exist
psql postgresql://audr:audr@localhost:5433/audr_test \
  -c "\dt balance_observation_invalidation history_point"

# Confirm new column on valuation_line
psql postgresql://audr:audr@localhost:5433/audr_test \
  -c "\d valuation_line" | grep observation_id
```

---

## Integration Tests

### Running

```bash
cd backend
# Unit + integration (requires the test database to be running)
TEST_DATABASE_URL=postgresql+psycopg://audr:audr@localhost:5433/audr_test \
  uv run pytest tests/integration/test_history.py tests/integration/test_reorg.py -v
```

### Test coverage summary

**test_history.py** — 12 tests

| Test | What it verifies |
|------|-----------------|
| `test_published_snapshot_rows_are_not_modified` | Snapshot row is read-only after publish |
| `test_valuation_lines_are_immutable_after_publication` | Documents application-layer immutability contract |
| `test_materialize_history_point_idempotent` | Second call returns existing row with `created=False` |
| `test_gap_markers_injected_for_temporal_discontinuities` | 72-hour gap produces ≥1 gap marker |
| `test_no_gap_markers_for_consecutive_hourly_snapshots` | Hourly snapshots produce no gap markers |
| `test_has_gap_true_for_partial_quality_snapshot` | `has_gap=True` for `quality='partial'` |
| `test_has_gap_false_for_complete_quality_snapshot` | `has_gap=False` for `quality='complete'` |
| `test_total_value_usd_summed_across_lines` | Aggregate sum of line values |
| `test_total_value_usd_null_when_all_lines_unpriced` | NULL when no lines are priced (stale) |
| `test_period_24h_excludes_older_points` | 24h filter excludes 48h-old snapshots |
| `test_cursor_pagination` | Pages without overlap; `next_cursor=None` on last page |
| `test_get_snapshot_detail_returns_observation_ids` | Lines include `observation_id` |
| `test_get_snapshot_detail_returns_none_for_unknown_snapshot` | 404 path |

**test_reorg.py** — 11 tests

| Test | What it verifies |
|------|-----------------|
| `test_invalidate_observation_inserts_record` | Invalidation record written with reason/depth |
| `test_invalidate_observation_idempotent` | Second call returns False, one DB row |
| `test_invalidation_reason_verification_pending` | `verification_pending` reason accepted |
| `test_invalidation_rejects_unknown_reason` | ValueError for bad reason |
| `test_history_point_marked_non_canonical_when_observation_invalidated` | `is_canonical=False` after reorg |
| `test_history_point_stays_canonical_when_unrelated_observation_invalidated` | Unrelated invalidation doesn't affect point |
| `test_recheck_canonicality_catches_late_invalidations` | Sweep detects late invalidations |
| `test_recheck_canonicality_no_false_positives` | No false flips |
| `test_schedule_replacement_scans_returns_affected_pairs` | Returns correct wallet/asset pair |
| `test_schedule_replacement_scans_deduplicates_pairs` | Two invalidations for same pair → one target |
| `test_schedule_replacement_scans_empty_when_all_canonical` | Empty list when no invalidations |

---

## Scale Fixture (AUD-87)

### What it generates

- 50 wallets
- 100 assets
- 500 held (wallet, asset) pairs
- 8,760 hourly `valuation_snapshot` + `valuation_line` + `history_point` rows

### Running

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://audr:audr@localhost:5433/audr_test \
  uv run python -m tests.fixtures.history_scale
```

### Expected output (reference run)

```
inserted 8760 snapshots in ~2–5s
inserted 4,380,000 valuation_lines in ~60–120s
inserted 8760 history_points in ~2–5s
total: ~70–130s depending on hardware
```

> **Note**: The fixture inserts ~4.4 million valuation_line rows (500 pairs × 8,760
> snapshots).  This is expected and exercises the query path at production scale.
> Total time scales with disk I/O; expect 2–5 minutes on spinning disks.

### Post-generation checks

```sql
-- Confirm counts
SELECT COUNT(*) FROM valuation_snapshot;  -- expect 8760
SELECT COUNT(*) FROM valuation_line;      -- expect 4,380,000
SELECT COUNT(*) FROM history_point;       -- expect 8760

-- Verify history query performance (should return in < 100ms)
EXPLAIN ANALYZE
  SELECT snapshot_id, snapshotted_at, total_value_usd, quality
  FROM history_point
  WHERE is_canonical = true
    AND snapshotted_at >= NOW() - INTERVAL '7 days'
  ORDER BY snapshotted_at DESC
  LIMIT 2000;
```

---

## API Contract Checks

### GET /history

```bash
# Requires a running server and authenticated session.
# With cookie and CSRF token set:
curl -b sid=<token> -H "x-csrf-token: <csrf>" \
  "http://localhost:8000/api/v1/history?period=7d"
```

Expected response shape:
```json
{
  "period": "7d",
  "entries": [
    {
      "snapshot_id": "...",
      "snapshotted_at": "2026-09-28T00:00:00+00:00",
      "total_value_usd": "1750000.0",
      "quality": "complete",
      "included_wallet_count": 50,
      "included_asset_count": 100,
      "has_gap": false,
      "is_canonical": true,
      "is_gap_marker": false
    }
  ],
  "next_cursor": null
}
```

### GET /history/{snapshot_id}

```bash
curl -b sid=<token> -H "x-csrf-token: <csrf>" \
  "http://localhost:8000/api/v1/history/<snapshot_id>"
```

Expected response shape:
```json
{
  "snapshot_id": "...",
  "snapshotted_at": "2026-09-28T00:00:00+00:00",
  "quality": "complete",
  "lines": [
    {
      "wallet_id": "...",
      "asset_id": "...",
      "token_address": "0x...",
      "symbol": "TK0",
      "raw_amount": "1000000000000000000000000",
      "block_number": 18000000,
      "price_usd": "1.5",
      "value_usd": "1500000.0",
      "observation_id": "..."
    }
  ]
}
```

---

## Known Limitations

1. **Gap threshold is computed per-query**: The gap threshold uses the median
   inter-point interval of the current result set.  For sparse datasets (< 2
   points), it falls back to the 4-hour floor.  This means gap marker placement
   may differ between the `all` and `7d` views if data density differs.

2. **observation_id is nullable on valuation_line**: Lines created by the
   snapshot publisher before migration 006 do not carry an `observation_id`.
   The `GET /history/{snapshot_id}` response will return `observation_id: null`
   for these legacy lines.

3. **Scale fixture does not generate balance_observation rows**: The fixture
   populates `valuation_line` without corresponding `balance_observation` rows,
   which means `observation_id` is NULL in all fixture lines.  Real production
   data will have observation IDs once the snapshot publisher is updated to
   populate `valuation_line.observation_id` (tracked as follow-up work).

4. **Canonicality recheck is linear**: `recheck_canonicality()` issues a single
   UPDATE that scans all canonical history_points.  For very large datasets
   (> 100k points) this may be slow; a partial index on
   `(is_canonical, snapshotted_at)` is already in migration 006 to mitigate this.

5. **No synthetic backfill**: The history query returns only actual recorded
   points.  A portfolio with gaps (e.g., downtime) will show discontinuities
   in the chart rather than interpolated values.  This is intentional: unknown
   history is never coerced to zero or a last-known value.

---

# Verification: Encrypted backup/restore drill (AUD-390)

**Date**: 2026-10-02
**Branch**: feat/aud-390-encrypted-backups
**Author**: Backend Developer

## Scope

AUD-390 added `scripts/backup.sh` and `scripts/restore.sh` (encrypted Postgres
dumps via `age`, with a `gpg --symmetric` fallback). This drill proves the
full cycle — backup, total data loss, restore — actually works, per the
acceptance criteria ("a restore drill is actually executed once against a
throwaway compose stack").

## Setup

A throwaway Compose project (`audr-backup-drill`, isolated secrets dir, own
named volume, own Docker network — no port or state shared with the real
`audr` project or any other running stack) was built from this repo's
`Dockerfile` (`db`, `migrate`, `api` services only; `web`/`worker` are not
needed to verify the data path). The owner was set up and one wallet added
through the real HTTP API (`POST /api/v1/setup`, `POST /api/v1/wallets`) —
not a raw SQL insert — so the drill exercises the same Argon2 password hash,
session/CSRF plumbing, and envelope-encrypted `key_state` row that a real
deployment would have.

## Steps and results

| Step | Command | Result |
|---|---|---|
| 1. Seed data | `setup` + `wallets` via API | Owner created, wallet `drill-wallet` (`0xd8dA6...a96045`) added |
| 2. Backup | `scripts/backup.sh` | Generated `secrets/backup_key.txt` (age identity) on first run, printed the public key, wrote `audr-<ts>.sql.age`, copied `master_key.hex` to a **separate** file |
| 3. Confirm ciphertext | `head -c200 *.sql.age` | Binary `age-encryption.org/v1` header — not readable SQL |
| 4. Confirm decryptable | `age -d -i backup_key.txt < dump` | Produces the plaintext `pg_dump` SQL header |
| 5. Destroy data | `docker compose down -v` | Named volume removed — full data loss, not just a schema wipe |
| 6. Restore refusal | `scripts/restore.sh dump.sql.age` (fresh DB, schema already migrated, no `--force`) | **Refused**: `"already has 32 table(s) ... Refusing to restore"`, exit 1 |
| 7. Forced restore | `scripts/restore.sh --force dump.sql.age` | Dropped/recreated `public` schema, restored cleanly with zero errors |
| 8. API healthy | `GET /health/live`, `GET /health/ready` | `{"status":"ok"}`; readiness reports `"key":"ok"` — the restored `key_state` row unwraps correctly under the unchanged `SECRET_KEY` |
| 9. Data intact | `POST /auth/login` (original password) + `GET /wallets` | Login succeeds; the same wallet (same id, address, label, `created_at`) is present |
| 10. gpg fallback | `backup.sh` with `age` removed from `PATH` | Falls back to `gpg --symmetric --cipher-algo AES256`, writes `.sql.gpg`, round-trips correctly with `gpg --decrypt` |

## Bug found and fixed during the drill

The first restore attempt (step 6) initially **did not refuse** on a non-empty
database — `scripts/restore.sh` read `0` tables immediately after
`docker compose up -d --wait db migrate` returned, even though `migrate` had
already applied all 16 Alembic revisions. Root cause: for a one-shot service
with `restart: "no"` and no `healthcheck`, `docker compose up --wait` is
satisfied once the container is **running**, not once it **exits** — so the
wait returned before `alembic upgrade head` had committed. This is a drill
artifact, not a bug in `backup.sh`/`restore.sh` themselves (in real use,
`migrate` always runs to completion before `api`/`worker` start, via
`depends_on: condition: service_completed_successfully`). Re-running the
drill with `docker compose run --rm migrate` (which blocks until the
container exits) instead of `up -d --wait` eliminated the race, and the
refuse-without-`--force` check then worked exactly as intended. Noted here so
nobody re-discovers this the hard way while scripting a restore runbook.

## Cleanup

`docker compose down -v --rmi local` removed all drill containers, the named
volume, and the two locally-built images. The drill ran entirely under
`$PAPERCLIP_RUN_SCRATCH_DIR` with its own `secrets/` directory — the repo's
real `secrets/db_password.txt` / `master_key.hex` were never read or written
by the drill.

## Conclusion

The encrypted backup → total volume loss → encrypted restore path is proven
end to end, including the safety refusal and the `gpg` fallback. See
[operations.md#backups](operations.md#backups) for the operator-facing
procedure and [security-at-rest-design.md](security-at-rest-design.md#e-encrypted-backups--done)
for why this closes item 2 of the at-rest backlog.

---

# Why `grep` over Postgres files and dumps is not evidence

Measured on a PostgreSQL 16 instance unrelated to audr, on 2026-10-03, while
checking whether a leaked credential had really been removed. Recorded here
because the conclusion applies to any claim of the form "I grepped the data
directory and it is clean".

## Scope

Two independent ways in which a `grep` that finds nothing runs over data that
is demonstrably still present.

## 1. The live heap — TOAST compression

Postgres stores wide `text`/`jsonb` values out-of-line in a TOAST table,
LZ-compressed. A row containing a 59-character marker string was present in
the live database — `SELECT ... WHERE result_json::text LIKE '%marker%'`
returned 1 row — while `grep -ra 'marker' <data-dir>` over the whole 526 MB
data directory returned **0 files**. The columns were `attstorage = x`
(extended) and large values compressed to ~0.32× their text length, so the
plaintext bytes never appear contiguously on disk.

## 2. The dumps — gzip, and encryption

The same marker in an hourly `*.sql.gz` dump: `grep -c` → **0**, `zgrep -c` →
**4**. audr's own `scripts/backup.sh` output is stronger still: `age`- or
`gpg`-encrypted, so a raw `grep` is guaranteed to find nothing regardless of
contents.

## What a scan has to do instead

```bash
# dumps: decompress or decrypt on the fly — never raw grep, never to disk
zgrep -c 'FINGERPRINT' dumps/*.sql.gz
age -d -i secrets/backup_key.txt backups/audr-*.sql.age | grep -c 'FINGERPRINT'
```

```sql
-- live DB: go through the engine, which decompresses TOAST transparently.
-- Cast jsonb/json columns to text, and check every column that can hold
-- captured process output, not just the obvious one.
SELECT count(*) FROM public.some_table
WHERE stdout_excerpt LIKE '%FINGERPRINT%'
   OR result_json::text LIKE '%FINGERPRINT%';
```

Two further traps:

- **Match on the secret's value, not its name.** Counting rows that contain
  the string `SECRET_KEY` measures how often the *variable* is mentioned,
  which includes every ticket, comment and transcript that merely discusses
  the leak — the scan inflates itself. Fingerprint the rotated value.
- **Scrubbing files is not scrubbing the database.** A value captured from
  process output lands in database columns as well as log files, and from
  there into every dump taken afterwards. Files need a rewrite; the database
  needs an `UPDATE`. Rotate the credential first, then scrub copies — clearing
  copies while the value is still live buys nothing.

---

# Verification: `pg_tde` spike (AUD-389 item 4)

**Date**: 2026-10-10
**Branch**: docs/aud389-pg-tde-spike
**Author**: infraLead

Timeboxed spike of Percona's `pg_tde` as an alternative/addition to the adopted
LUKS/ZFS baseline. The conclusion and the reasoning live in
[security-at-rest-design.md](security-at-rest-design.md#spike-results-item-4-2026-10-10);
this is the record of what was actually run, so the spike does not have to be
redone to check a claim.

## Setup

Throwaway containers, no audr code and no audr database involved. Image
`percona/percona-distribution-postgresql:17`, digest
`sha256:b5e66df1a76d7309241e03d1bc741b53d2d867f27e21389e0512c81d05024969`,
reporting `postgres (PostgreSQL) 17.11 - Percona Server for PostgreSQL 17.11.1`,
with `pg_tde.so` and extension scripts up to `pg_tde--2.1--2.2.sql` present in
the image. Data directory and keyring each on their own named Docker volume;
both volumes and the image were removed afterwards.

```bash
docker run -d --name tde -e POSTGRES_PASSWORD=... -e POSTGRES_DB=audr \
  -v tde_data:/data/db -v tde_kr:/keyring \
  percona/percona-distribution-postgresql:17 \
  -c shared_preload_libraries=pg_tde
# the keyring volume must be owned by `postgres` and mode 700, or provider
# creation fails with "Failed to open keyring file ...: Permission denied"
```

## Steps and results

### 1. Encrypted heap vs plain heap, on the same data

Two tables in one database, same column types, same length of marker value, one
`USING tde_heap` and one default `heap`; `CHECKPOINT` to force the pages out.

| What | Result |
|---|---|
| `grep SENTINELENC <datadir>/base/16384/16433` (tde_heap) | 0 matches |
| `grep SENTINELPLAIN <datadir>/base/16384/16440` (heap) | 1 match |

The plain table is the **positive control**, and it is what makes this a valid
test rather than the mistake recorded in
[Why `grep` over Postgres files and dumps is not evidence](#why-grep-over-postgres-files-and-dumps-is-not-evidence):
the markers are short values stored inline, not TOASTed, so a plaintext hit is
expected — and it does hit, in the same byte-scan, for the unencrypted table.
A clean scan of only the encrypted file would have proved nothing.

### 2. WAL

`pg_dump` of the TDE database is plaintext SQL (2 marker hits) — as designed;
dumps are covered by the encrypted-backup work in item 2, not by TDE.

WAL is **not** encrypted by table access method. With `pg_tde.wal_encrypt` off,
the marker written into a `tde_heap` table appears in
`pg_wal/000000010000000000000001`. With it on, 0 WAL files contain it.

Enabling it on a fresh cluster is a two-phase boot:

```
FATAL:  principal key not configured
HINT:  Use pg_tde_set_server_key_using_global_key_provider() to configure one.
```

The server key is set by a SQL function, so the server has to be up first.
Working order: boot with `wal_encrypt` off → `CREATE EXTENSION pg_tde` →
`pg_tde_add_global_key_provider_file` + `pg_tde_create_key_using_global_key_provider`
+ `pg_tde_set_server_key_using_global_key_provider` → restart with
`-c pg_tde.wal_encrypt=on`. Verified: after the restart `SHOW
pg_tde.wal_encrypt` is `on`, writes succeed, and no WAL file carries the
plaintext.

### 3. No application change, and reversible

- `ALTER DATABASE restored SET default_table_access_method = tde_heap`, then
  restoring a **plain** `pg_dump` into it: the restored table comes back as
  `tde_heap` (checked via `pg_class.relam` → `pg_am.amname`). This is the PG16 →
  PG17 migration path, and it reuses `scripts/backup.sh` / `scripts/restore.sh`
  rather than needing `pg_upgrade` across two different distributions.
- `ALTER TABLE w SET ACCESS METHOD heap` converts an encrypted table back;
  `relam` reads `heap` afterwards. Rollback is per table.

### 4. Keyfile loss

`mv /keyring/db.key /keyring/db.key.bak` and restart: the **server starts
normally** (`State.Running = true`) and the failure only shows at read time —

```
ERROR:  key "db_key" not found in key provider "d_kr"
```

Moving the 292-byte keyfile back and restarting restored the row intact. So the
keyring is a backup-and-custody obligation on the same footing as
`secrets/master_key.hex`, and its loss presents as a healthy database that
cannot read itself.

### 5. Overhead

`pgbench -i -s 10`, then 4 clients / 2 threads, on the same container, WAL
encryption on for both databases (so this isolates the `tde_heap` page cost):

| Workload | `heap` | `tde_heap` | Delta |
|---|---|---|---|
| default write mix, 20s | 1084 tps (3.69 ms avg) | 991 tps (4.04 ms avg) | **-8.6%** |
| `-S` read-only, 15s | 13543 tps | 13748 tps | within noise |

The read-only figure is not evidence of free reads in general — scale factor 10
fits in shared buffers, so decryption is mostly skipped on buffer hits. It does
say that TDE is not a read-path problem at audr's data size.

## Cleanup

Containers (`docker rm -f`), both named volumes (`docker volume rm`) and the
Percona image (`docker rmi`) were removed. Nothing in the audr stack, the
staging host or the repository's compose files was touched — no compose overlay
was added, deliberately, since adding one would imply an adoption that did not
happen.

## Conclusion

`pg_tde` is usable and the integration is smaller than feared (no schema or
query changes, dump/restore migration, per-table rollback). It is **not
adopted**: with a local keyfile it adds nothing over LUKS against whole-machine
theft, and the version that does add something needs Vault/KMIP. See the design
doc for the full argument and the conditions that would reopen it.

---

# Verification: what the worker actually reads, and what an encrypt-a-column migration leaves on disk (AUD-389 item 5 prep)

**Date**: 2026-10-10
**Branch**: docs/aud389-worker-plaintext-audit
**Author**: infraLead

[security-at-rest-design.md](security-at-rest-design.md#4-the-key-model-decision--settled-2026-10-11-option-1)
§4 option 3 ("two-tier keys") rested on one unmeasured claim — that the
owner-identifying columns are ones *"the worker arguably does not need in
plaintext"* — and listed "a careful audit of what the worker actually reads"
as its outstanding cost. This is that audit. It also checks the adjacent
assumption nobody had tested: that migrations 0021/0022 removed the plaintext
they replaced from disk, rather than merely from the schema.

Both answers came out against the shipped story, so they are recorded here
rather than only in the design doc.

## Scope

1. Which worker job kinds need a plaintext `wallet.address`, by call graph.
2. Whether `ALTER TABLE ... DROP COLUMN` after an in-place encrypting `UPDATE`
   actually takes the plaintext off disk (throwaway container, with a control).
3. The same question on the live staging stand, post-0022.
4. Whether any *other* table still stores the owner's own address in plaintext.

## 1. What the worker reads

Call graph from `audr/jobs/__main__.py`, one row per job kind the worker
dispatches:

| Job kind | Needs plaintext `wallet.address`? | Why |
| --- | --- | --- |
| `BALANCE_SCAN` | **Yes, unavoidably** | The address *is* the RPC parameter: `jobs/__main__.py:271` reads `wallet.address`, which reaches `eth_getBalance` / `balanceOf` via `_read_and_record`. No ciphertext substitution exists — the chain only answers to the address. |
| `DISCOVERY` | **Yes, unavoidably** | `list_wallets` → `wallet.address` for the RPC scan, and the per-run checkpoint is sub-keyed by address (`jobs/__main__.py:136-142`). |
| `EVENT_INDEXER` | **Yes, unavoidably** | `jobs/event_indexer.py:362-369` decrypts every active wallet's address, then pads it into the `Transfer`/`Approval` log topic filter. The filter is the address. |
| `QUOTE_REFRESH` | No | Reads `asset.token_address` only — token contract addresses, public by nature (`jobs/quotes.py:278-290`). |
| `NEWS_REFRESH` | No | No wallet access at all. |
| `ASSET_ICON_REFRESH` | No | Token addresses only. |
| `VALIDATE_RPC` / `VALIDATE_QUOTES` | No | Provider credentials only. |

Two incidental results worth keeping:

- **AUD-490 already removed the avoidable reads.** `BALANCE_SCAN`'s
  monitored-token lookup keys on `wallet_id`, and `publish_valuation_snapshot`
  never decrypts an address — only the API-side
  `get_latest_snapshot_lines` does (`portfolio/snapshot.py:221`). So what
  remains is not laziness; it is the set where plaintext is load-bearing.
- **`wallet.label` is the one owner-identifying field the worker reads and does
  not use.** `wallets.service.list_wallets` decrypts label and address together
  (`wallets/service.py:129-130`); no job touches the label.

**Conclusion for §4 option 3:** the premise is false as written. Three of the
worker's eight job kinds — and precisely the three that make audr a tracker
rather than a viewer — cannot run without the plaintext address. A
password-derived tier over the owner-identifying set therefore does not buy
"background work keeps running"; it degrades to option 2 for everything except
`wallet.label`, which is cosmetic. Option 3's remaining honest form is a tier
that holds *only* the label, which is not worth a second key.

## 2. `DROP COLUMN` does not take the plaintext off disk

Throwaway `postgres:16-alpine` container, no audr code or data, removed
afterwards. The second row is the control — a value whose column is *not*
dropped, so a scan finding nothing would have to be explained:

```sql
CREATE TABLE w (id int primary key, address text, ct bytea);
INSERT INTO w VALUES (1,'0xdeadbeefMARKERplaintextADDR1111111111aa', NULL);
INSERT INTO w VALUES (2,'0xCONTROLnotdroppedMARKER22222222222222bb', NULL);
UPDATE w SET ct = decode('aabbcc','hex');   -- mirrors 0021/0022's in-place encrypt
ALTER TABLE w DROP COLUMN address;
CHECKPOINT;
```

```console
$ strings base/16384/16385 | grep -c MARKER
4                      # both rows, two tuple versions each (pre- and post-UPDATE)
$ grep -c MARKER pg_wal/000000010000000000000001
2
```

`DROP COLUMN` is a catalog operation: it sets `attisdropped` and stops
returning the column. The bytes stay in every existing tuple, and the
superseded row versions left by the encrypting `UPDATE` keep a full plaintext
copy until they are vacuumed away — which `VACUUM` alone does not guarantee to
remove from the page image.

Remediation, verified in the same container:

```console
$ psql -c "VACUUM FULL w;" -c "SELECT pg_relation_filepath('w');" -c "CHECKPOINT;"
base/16384/16392       # new relfilenode: the table was rewritten
$ strings base/16384/16392 | grep -c MARKER
0
$ ls base/16384/16385
ls: No such file or directory          # old heap unlinked
$ grep -rl MARKER $PGDATA
/var/lib/postgresql/data/pg_wal/000000010000000000000001   # WAL still holds it
```

So a table rewrite (`VACUUM FULL`, or `pg_repack` where the exclusive lock is
unacceptable) is what actually removes it from the heap. Two residues survive
even that: the retained WAL segments, until they recycle, and the unlinked
heap's blocks on the raw device, until something overwrites them. Neither is
scrubbable from SQL — which is the argument for the volume-level baseline, not
against the column work.

Note `VACUUM FULL` cannot run inside a transaction block, so this cannot be
appended to the Alembic migration that creates the need for it. It is an
operator step: [operations.md](operations.md#scrubbing-plaintext-left-by-an-encrypt-a-column-migration).

## 3. On the live staging stand, post-0022

`192.168.1.228`, `audr-db-1`, after migration 0022 had been deployed and the
plaintext `wallet.address` column dropped:

```console
$ psql -At -c "CHECKPOINT" -c "SELECT pg_relation_filepath('wallet')"
base/16384/16611
$ strings base/16384/16611 | grep -coE "0x[0-9a-fA-F]{40}"
15
$ strings ... | grep -oE "0x[0-9a-fA-F]{40}" | sort | uniq -c
      4 0x9b061f…
      1 0xab5801…
     10 0xd8da6b…
```

Three distinct real addresses, in plaintext, in the `wallet` heap of a stand
whose schema says addresses are encrypted. `pg_stat_user_tables` showed
`n_dead_tup = 4` on a two-row table — the 0021/0022 `UPDATE`s' superseded
versions. Fixed in place:

```console
$ psql -At -c "VACUUM (FULL, ANALYZE) wallet" -c "SELECT pg_relation_filepath('wallet')" -c "CHECKPOINT"
base/16384/27720
$ strings base/16384/27720 | grep -coE "0x[0-9a-fA-F]{40}"
0
$ ls base/16384/16611 && echo present || echo unlinked
unlinked
```

The retained WAL still contains all three addresses (`grep -ac` hits in 5 of
the 5 retained segments for one of them), as §2 predicts — and will keep
containing them regardless, because of §4.

Addresses are masked here deliberately; they are the founder's own wallets.
The scan counted a 40-hex-digit pattern rather than a known value, so it is a
discovery scan, not a confirmation of something already known. It is also
specifically *not* the mistake recorded in
[why grep is not evidence](#why-grep-over-postgres-files-and-dumps-is-not-evidence):
a scan finding plaintext is positive evidence, whereas that section is about
a scan finding *nothing* being worthless.

## 4. `onchain_event` stores the owner's address in plaintext anyway

`onchain_event.from_address` and `to_address` are plain `text`
(`backend/migrations/versions/0004_onchain_events.py:38-39`), and
`_insert_event` only writes a row when one of them *is* the tracked wallet:
`transfer_out` requires `from_addr == wallet_norm`, `transfer_in` requires
`to_addr == wallet_norm`, `approval` requires the owner topic to match
(`jobs/event_indexer.py:299-327`). Every row therefore carries the owner's own
address in plaintext, by construction.

The staging stand has 12 such rows today, so this is live, not theoretical.

This means **AUD-490 did not take the owner's addresses off disk** on any stand
with event history — it took them out of one table. The design doc's framing of
3b as "the last owner-identifying column still stored as plaintext" was wrong,
and so was migration 0022's docstring repeating it. Backlog item 3c covers the
fix; the cheap shape is to stop storing the owner's side at all, since
`event_type` plus `wallet_id` already determines which side it was.

## Cleanup

The throwaway container was `docker rm -f`'d. On the stand, the only change was
`VACUUM (FULL, ANALYZE) wallet` — a 2-row table, sub-second exclusive lock, no
schema or data change. Nothing else on the host was touched.

## Conclusion

Option 3 as described in §4 is not available: the worker needs plaintext
addresses for balance scanning, discovery and event indexing, so the realistic
choice is option 1 or option 2. Separately, two things the at-rest work claimed
were closed are not: an encrypting migration leaves the plaintext in the heap
until the table is rewritten (fixed on the stand, documented as an operator
step), and `onchain_event` still writes the owner's address in plaintext on
every indexed event (backlog item 3c).

---

# Verification: Scheduled backup, and a live-artefact restore drill (AUD-562)

**Date**: 2026-10-15
**Branch**: fix/aud-562-scheduled-backups
**Author**: infraLead
**Host**: the owner stand, 192.168.1.228

## Scope

AUD-562 installed a daily encrypted dump on the owner stand, which until that
day had never been backed up at all: no `backups/` directory, no dump anywhere
on the host, no timer, `crontab` not even installed. The `db_data` volume was
the only copy of a live portfolio with its full valuation history and — since
AUD-490 — wallet addresses that exist nowhere else in plaintext.

AUD-390 had already proved the *scripts* worked, in a throwaway stack on
synthetic data. This drill proves something the earlier one could not: that
**the specific file the timer wrote, on the real host, restores the real
database**. Working tooling and a restorable backup are different claims.

## What was installed

| Piece | Where |
|---|---|
| `audr-backup.timer` → `audr-backup.service` | `/etc/systemd/system/`, rendered from `ci/host-units/audr-backup.service.in` |
| `backup-run.sh`, `backup.sh`, `dump-prune.py`, `restore.sh`, `notify.sh`, `notify-board.py` | pinned copies in `~/.local/share/audr-backup/scripts/` |
| `age` 1.0.0 | `apt install age` — the host had only `gpg`, so dumps would otherwise have taken the fallback path |

`OnCalendar=03:20:00`, `Persistent=true`, `RandomizedDelaySec=10m`, retention
`--days 7 --weeks 4`. Installed by `scripts/install-backup.sh`, which is
idempotent, has a `--check` mode, and runs the real unit once at the end so the
install either produces a verified dump or fails where someone is watching.

## The artefact

The first run was the systemd unit itself, not a hand-typed command:

```
$ sudo systemctl start audr-backup.service
Backup written: /home/codex/audr/backups/audr-20261015-142732.sql.age
KEK not copied (AUDR_BACKUP_SKIP_KEY_COPY=1); it stays only in /home/codex/audr/.env (SECRET_KEY).
[backup-run] verifying ... decrypts (295260620 bytes)
[backup-run] decrypt OK — header looks like pg_dump output
```

295,260,620 bytes (282 MiB), `sha256 2639c965fec3df3f…`, mode `600`. Note the
KEK line: before this change `backup.sh` printed `WARNING: secrets/master_key.hex
not found — provider credentials cannot be recovered`, naming a path that is
absent by design on a deploy-style host while the key sat in `.env` all along.

## Steps and results

| Step | Command | Result |
|---|---|---|
| 1. Exact live counts | `query_to_xml` over every `public` table | 35 tables. `pg_stat_user_tables.n_live_tup` was **not** used — it is a statistics estimate and read `0` for `owner`, `key_state`, `integration` and `alembic_version`, all of which have rows |
| 2. Isolated target | `docker compose -p audr-restore-drill up -d --wait` | `db` only, `postgres:16-alpine`, own project, own volume (`audr-restore-drill_drill_db_data`), own network, **no published port**. Target confirmed empty: `0` tables in `public` |
| 3. Restore the artefact | `AUDR_COMPOSE_PROJECT=audr-restore-drill restore.sh <dump>` | Exit 0, **zero** `ERROR:` lines from `psql`. No `--force` needed, so this was a restore into a genuinely empty database, not over a migrated schema |
| 4. Row counts | same query against the restored DB, plus live counts again afterwards | **34 of 35 tables matched the live database exactly.** `job_run` restored 901,310 against 901,313 live-before / 901,314 live-after |
| 5. KEK-wrapped ciphertext | `md5(string_agg(...))` over `key_state.wrapped_key`, `wallet.{address,label,bidx}`, `integration.encrypted_blob` | Byte-identical, live vs restored, in all three |
| 6. Real decrypt | app's own `get_master_key` / `decrypt_address` in the backend image, `SECRET_KEY` taken from the host's `.env`, pointed at the restored DB | Master key unwrapped (32 bytes); both wallets' addresses and labels decrypted to their real values; `balance_observation` 121,653 rows; latest `history_point.total_value_usd` = 127598.540250440065728237 |
| 7. Pruner | `dump-prune.py --prefix audr --days 7 --weeks 4 --apply` | `1 file, 0.27 GiB, keep 1, delete 0` — correct for a directory holding one dump |

### On the one table that did not match

`job_run` is append-only and the worker writes to it continuously. The drill
captured live counts **before and after** the restore specifically to tell drift
from loss: 901,310 restored ≤ 901,313 before ≤ 901,314 after. The dump is a
consistent `pg_dump` snapshot taken at 14:27:32 and the live table has only
grown since. Every other table, including every table holding owner data, was
identical.

Step 6 is the one that matters most and the one AUD-390 could not perform: it
proves the KEK resolution this change added — `.env`'s `SECRET_KEY`, on a host
with no `secrets/master_key.hex` — is the *right* key for this artefact. A
restore that returns rows nobody can decrypt is not a recovery.

## Sizing, and why the weekly tier is capped

One dump is 282 MiB: `age` does not compress and neither does plain-text
`pg_dump`. The host has ~11 GiB free on `/`.

`dump-prune.py`'s weekly tier was **unbounded** — the newest dump of every older
ISO week, kept forever. At 95 MB/dump on the Paperclip archive that was
tolerable; at 282 MiB a week it adds ~14.7 GiB/year and fills this disk in
roughly eight months. A host that runs out of space because of its own backups
is a worse outage than the one they prevent, and it arrives silently. So
`--weeks` was added (default uncapped, so the Paperclip timer is unchanged) and
the stand runs `--days 7 --weeks 4`: 11 files, ~3.1 GiB, flat.

## Cleanup

`docker compose -p audr-restore-drill down -v --remove-orphans` removed the
drill container, volume and network; `/tmp/aud562` was deleted. The live stack
was never a target of any command in this drill: `audr-db-1` stayed up
throughout (8 days at the time), `audr_db_data` was untouched, and the only
reads against it were `pg_dump` and the count/checksum queries above.

## Conclusion

The owner stand now has a scheduled, pruned, self-verifying encrypted backup,
and one dump of the live database has been proven restorable end to end —
including decryptable, which is the half that a row count alone would have
missed.

What this does **not** cover is loss of the host, and that is deliberate: the
dumps and the key that makes them readable both live on the stand, and the
scheduled path writes no KEK copy beside the dump (AUD-426 removed exactly such
a duplicate from this directory). Carrying a dump and the `SECRET_KEY` off-host,
stored apart, remains an owner action; `docs/operations.md#backups` states the
boundary rather than implying a green timer covers it.

---

# Verification: Compressed dumps, and a two-shape restore drill (AUD-564)

**Date**: 2026-10-15
**Branch**: fix/aud-564-compress-dumps
**Author**: infraLead
**Host**: the owner stand, 192.168.1.228

## Scope

`scripts/backup.sh` piped `pg_dump --format=plain` straight into `age`. Neither
stage compresses, so AUD-562 measured a 282 MiB dump of a 351 MB database and
every other constraint on the stand's backups was sized against that number.
This change puts `gzip -6` between the dump and the encryptor —
`audr-<ts>.sql.gz.age` — and teaches `restore.sh` the matching step.

The risk in this change is entirely in the restore path: making *new* dumps
smaller while silently breaking the restore of *existing* ones would be
strictly worse than large dumps. So the gate was a real restore of **both**
shapes, not just the new one.

## The win, measured

| | Dump | Size |
|---|---|---|
| Before (AUD-562, uncompressed) | `audr-20261015-142732.sql.age` | 295,260,620 B — 282 MiB |
| After (this change) | `audr-20261015-172140.sql.gz.age` | 51,621,096 B — **49 MiB** |

**5.7x**, not the 5-10x the issue estimated from first principles, and the
measured figure is what the comments and docs now carry. Retention stays at
`--days 7 --weeks 4`: the same window now costs ~0.6 GiB instead of ~3.1 GiB.
Widening the recovery window is a separate decision and was deliberately not
smuggled into the change that made it cheap.

## Steps and results

Each shape was restored into its **own** isolated throwaway stack —
`postgres:16-alpine`, own compose project, own volume, own network, no published
port — and each target was confirmed to have `0` tables in `public` before the
restore, so both were restores into genuinely empty databases rather than over a
migrated schema.

| Step | Command | Result |
|---|---|---|
| 1. Live counts | `query_to_xml` over every `public` base table | 35 tables, captured before the dump |
| 2. New shape restores | `restore.sh audr-20261015-172140.sql.gz.age` into `audr-drill-564gz` | Exit **0**, **zero** `ERROR:` lines from `psql` |
| 3. Row counts | same query against the restored DB | **35 of 35 tables matched the live database exactly**, including `job_run` at 901,380 |
| 4. Distinct databases | `pg_postmaster_start_time()` on both | Drill `2026-10-15 17:24:26`, live `2026-10-07 10:30:03` — the counts above were read from the drill, not from live by accident. Live `job_run` moved to 901,381 during the drill, as an append-only table should |
| 5. KEK-wrapped ciphertext | `md5(string_agg(...))` over `key_state.wrapped_key`, `wallet.{address_ciphertext,label_ciphertext,address_bidx}`, `integration.encrypted_blob` | Byte-identical, live vs restored, all three |
| 6. Real decrypt | app's own `get_master_key` / `decrypt_address` / `decrypt_label` from the running backend image, `SECRET_KEY` from the host's `.env`, pointed at the restored DB | Master key unwrapped (32 bytes); both wallets' addresses and labels decrypted to their real values; `balance_observation` 124,101 rows; latest `history_point` `quality = complete`, `total_value_usd` = 133228.688387555993181907 |
| 7. **Old shape still restores** | `restore.sh audr-20261015-142732.sql.age` (the pre-change 282 MiB dump) into `audr-drill-564old` | Exit **0**, **zero** `ERROR:` lines |
| 8. Old shape decrypts | steps 5–6 repeated against it | Ciphertext checksums identical to live in all three tables; addresses and labels decrypted; `balance_observation` 121,653, `total_value_usd` = 127598.540250440065728237 — **exactly the figures AUD-562 recorded for this artefact**, which independently confirms the old dump is intact and the new `restore.sh` reads it unchanged |
| 9. Pruner on mixed names | `dump-prune.py --prefix audr --days 7 --weeks 4` (dry run) | `3 files, 0.82 GiB, keep 3, delete 0` — the old `.sql.age` dumps are still recognised as dumps |
| 10. Unit test suite | `pytest scripts/test_dump_prune.py` | 22 passed, including the new cases for `.sql.gz.age` / `.sql.gz.gpg`, the rejection of `encrypt-then-compress` (`.sql.age.gz`) and `.part` names, and a mixed-shape directory pruning as one series |

Step 8 is the one that justifies this change shipping at all. Step 7 proves the
old dump *loads*; step 8 proves it loads the *right bytes*, with the host's KEK
still the right key for them.

## A note on the pruner regex

The pattern went from `\.sql(?:\.gz|\.age|\.gpg)?$` to
`\.sql(?:\.gz)?(?:\.age|\.gpg)?$`. The old one admitted `.gz` **or** an
encryption suffix but not both, so `audr-<ts>.sql.gz.age` would have matched
nothing — and a file that is not a dump is never a candidate, so the `--weeks`
cap would have silently stopped bounding the one directory AUD-562 added it to
bound. The disk-full it exists to prevent would have arrived anyway, with a
green timer the whole way.

## An incident worth recording

The first run of this drill was destroyed mid-restore: `psql` died with
`FATAL: terminating connection due to administrator command` and the drill
container, network and volume were all gone. The cause was a **concurrent
sibling run of this same agent** performing the same drill under the same
compose project name and running its `down -v` cleanup while this restore was in
flight. Nothing touched the live stack (`audr-db-1`, 8 days up, `audr_db_data`
untouched; the only reads against it were `pg_dump` and the count queries). The
fix is procedural and cheap: give a throwaway stack a **run-unique** project
name, not a task-unique one.

## Cleanup

Both drill stacks were removed with `down -v --remove-orphans`, and
`/tmp/aud564` deleted. `install-backup.sh` was then re-run so the pinned copies
on the host match this branch — without that step the host keeps writing the old
uncompressed shape no matter what this repo says.

---

# Verification: Compressed dumps, and a two-shape restore drill (AUD-564)

**Date**: 2026-10-15
**Branch**: fix/aud-564-compress-dumps
**Author**: infraLead
**Host**: the owner stand, 192.168.1.228

## Scope

AUD-564 put `gzip` between `pg_dump` and `age` in `scripts/backup.sh`. The
saving is real but it is not the interesting part of this drill. The risk in
this change is entirely on the restore side: a change that makes *new* dumps
smaller while quietly breaking the restore of the *existing* ones is strictly
worse than large dumps, because the three dumps already on the stand are the
only copy of the days they cover.

So the gate was both shapes, each restored end to end from the real live
database — not the new one alone.

## The measurement

| | Dump | Size |
|---|---|---|
| Before | `audr-20261015-144024.sql.age` | 295,261,776 B (**282 MiB**) |
| After | `audr-20261015-172143.sql.gz.age` | 51,621,096 B (**49 MiB**) |

**5.7x**, not the 5-10x the issue estimated from typical SQL. Worth recording
as the measured number rather than the guess: a large share of this database is
`balance_observation` and `job_run`, whose numeric and UUID columns compress
far less well than the schema text does.

## Method

Both runs used the same isolated target as the AUD-562 drill: `postgres:16-alpine`
in its own compose project, its own volume, its own network, no published port.
The live stack was never a target — `audr-db-1` stayed up throughout, and the
only reads against it were `pg_dump` and the count/checksum queries.

Row counts came from `query_to_xml` over every `public` table, not
`pg_stat_user_tables.n_live_tup`, which is an estimate and reads `0` for
several tables that have rows.

## Results

| Step | `.sql.gz.age` (new) | `.sql.age` (pre-AUD-564) |
|---|---|---|
| Target empty before restore | `0` tables | `0` tables |
| `restore.sh` exit | 0, **zero** `ERROR:` lines | 0, **zero** `ERROR:` lines |
| Tables restored | 35 | 35 |
| Row counts vs live | **all 35 identical** | all 35 identical to that dump's snapshot |
| `key_state.wrapped_key` md5 | `41b893ad…` — matches live | `41b893ad…` — matches live |
| `wallet` ciphertext md5 | `b6bc20be…` — matches live | `b6bc20be…` — matches live |
| `integration.encrypted_blob` md5 | `09049b02…` — matches live | `09049b02…` — matches live |
| Master key unwrap (app's `get_master_key`, `SECRET_KEY` from the host `.env`) | 32 bytes | 32 bytes |
| Wallet addresses (app's `decrypt_address`/`decrypt_label`) | both decrypted to their real values | both decrypted to their real values |
| `/health/ready` | `key: ok` | `key: ok` |

The new shape matched live on **all 35 tables**, including `job_run`, which the
AUD-562 drill could not match because the worker appends to it continuously —
here the comparison snapshot happened to fall between two writes. Live drifted
by one `job_run` row during the drill, which is the same append-only churn.

`/health/ready` on the old-shape restore also reported `worker: stopped` and
`quotes: degraded`. Both are expected: no worker runs in a drill stack, and
that dump's quote data was already hours stale. `key: ok` is the field this
gate is about — it is the one that distinguishes a restore that returns rows
from a restore that returns rows anyone can read.

## The pruner, on a directory holding both shapes

`dump_re()` now treats compression and encryption as independent optional
suffixes, so `.sql`, `.sql.gz`, `.sql.age`, `.sql.gz.age`, `.sql.gpg` and
`.sql.gz.gpg` are all dumps. This matters beyond tidiness: a pattern that only
matched the new shape would classify every pre-AUD-564 dump as "not a dump" —
never counted, never pruned — so `--weeks` would silently stop bounding the
directory it was added to bound, and the disk-full it prevents would arrive
anyway, from the old big files.

Dry run against a synthetic mixed directory (`--days 7 --weeks 2`, today
2026-10-15) kept one daily, promoted one weekly survivor of **each** shape
(`audr-20261008-033012.sql.gz.age`, `audr-20261001-033012.sql.age`), deleted
two, and left `master_key-20261015-033012.hex` alone as "not a dump". Against
the real stand directory it saw all three existing dumps and deleted nothing.

## Cleanup

The drill project was removed with `down -v --remove-orphans` and `/tmp/aud564`
deleted. Two further drill stacks from earlier in this change
(`audr-drill-564gz`, `audr-drill-564old`) were found still running and were
removed with their volumes and networks; `/` went back to 11 GiB free. The
three pre-existing dumps in `~/audr/backups` were not touched by any step.

## Conclusion

The stand's dumps are 5.7x smaller, and both the new and the old shape have
been proven restorable *and decryptable* from the real live database. The
retention defaults (`--days 7 --weeks 4`) were deliberately left unchanged:
this change made the recovery window cheaper to hold, and widening it is a
separate decision.
