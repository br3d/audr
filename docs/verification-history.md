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
