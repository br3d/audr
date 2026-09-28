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
