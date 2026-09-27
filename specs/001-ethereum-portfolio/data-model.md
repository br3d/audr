# Data Model: Ethereum Portfolio

## Conventions and invariants

PostgreSQL 18, UUID row IDs except singleton keys, timestamptz UTC timestamps.
Normalize addresses to 20-byte values; render checksum form only for display.
Network is chain_id=1. Asset identity is native ETH or contract, never ticker.
Raw units use NUMERIC(78,0), integer only, 0 <= value <= 2^256-1.
Metadata decimals are nullable integers 0–255. Monetary values use canonical decimal
strings inside encrypted, schema-versioned payloads; API responses decrypt to strings.
No NaN/infinity, float conversions or implicit metadata defaults in accounting.

Encryption envelope: key_version, nonce, ciphertext, payload_schema; associated data
binds table, row ID and purpose. Encrypt secrets and provider-derived quote/valuation
payloads. IDs, source, status and timestamps remain queryable for history/provenance.
Raw onchain balances are exact numeric columns, not encrypted quote data.
Password and session digests are non-reversible and are never encrypted for recovery.

## Entities

| Entity | Principal fields | Constraints and lifecycle |
|--------|------------------|---------------------------|
| owner | id=1, password_hash, created_at, password_changed_at | Singleton; created atomically once; reset updates hash and revokes sessions |
| session | id, token_digest, csrf_digest, owner_id, created_at, last_seen_at, absolute_expires_at, revoked_at | Unique digest; opaque token never persisted; idle expiry 1 hour, absolute 12 hours |
| auth_throttle | bucket_key_digest, window_start, failure_count, blocked_until | Persistent account/IP buckets; no plaintext submitted credentials |
| integration | id, kind, provider, encrypted_config, config_revision, enabled, health, last_validated_at | At most one active RPC and one active quote connection; API exposes redacted summary only |
| schedule | id, kind, interval_seconds, enabled, next_due_at, revision | Separate balances, discovery, quotes; validated minimums; revisions prevent lost updates |
| provider_budget | integration_id, period_start, attempt_count, logical_call_count, local_limit, per_second_limit, cooldown_until | Counts are transactionally reserved before dispatch; no claim of provider-billed currency |
| wallet | id, address, label, created_at, tracking_active | Unique(chain_id,address); stop tracking is soft removal, not history deletion |
| membership_revision | id, created_at, reason | Frozen wallet membership and asset-exclusion revision for a valuation |
| membership_wallet | revision_id, wallet_id | Primary key pair; immutable historical membership |
| asset | id, chain_id, kind, contract_address, symbol, name, decimals, metadata_source, metadata_status, excluded | Unique contract identity; single native row via partial unique index; native address is null |
| asset_metadata_revision | id, asset_id, decimals, name, symbol, source, observed_at, owner_confirmed | Immutable interpretation used by observations; later changes cannot rescale old history |
| membership_exclusion | revision_id, asset_id | Exclusion set frozen for history; visibility and valuation inclusion are separate |
| catalog_version | id, upstream_url, upstream_commit, content_sha256, bundled_at, entry_count, license | Exact version embedded with application; no live upstream dependency |
| catalog_entry | catalog_version_id, asset_id, normalized_metadata | Unique pair; invalid input excluded with import report |
| manual_token | asset_id, created_at, enabled, metadata_override_revision_id | Independent of catalog; merged by asset ID in scan scope |
| monitored_pair | wallet_id, asset_id, discovered_at, last_discovery_run_id, enabled | Unique pair; once discovered remains polled even after a zero observation |
| discovery_coverage | wallet_id, catalog_version_id, run_id, started_at, completed_at, attempted, succeeded, failed, remaining, status | Partial versus complete explicitly distinguished; no claim of all possible tokens |
| job_run | id, kind, trigger, dedupe_key, state, attempt, next_attempt_at, lease_token, lease_until, heartbeat_at, scope_revision, progress, safe_error | Unique dedupe key; one active job per kind; checkpointed by wallet/chunk |
| discovery_candidate | run_id, wallet_id, asset_id, block_number, block_hash, raw_balance, status | Temporary discovery evidence, not a simultaneous portfolio balance set |
| balance_set | id, job_run_id, scope_revision, block_number, block_hash, block_time, observed_at, status, finalized_at | One committed set per job; may be partial; invalidation recorded separately |
| balance_observation | id, balance_set_id, wallet_id, asset_id, metadata_revision_id, raw_balance, status, safe_error | Unique(set,wallet,asset); raw balance null on failure, nonnegative integer on success |
| quote_set | id, job_run_id, integration_id, provider, fetched_at, status | One per quote job; never relies on ticker matching |
| quote_observation | id, quote_set_id, asset_id, provider_time, fetched_at, status, encrypted_price, safe_error | Unique(set,asset); preserve missing/error separately from valid zero price |
| valuation_snapshot | id, snapshot_key, balance_set_id, quote_set_id, membership_revision_id, created_at, encrypted_summary, quality | Unique input-derived key; quality includes missing/stale/mixed observations and discovery scope |
| valuation_line | snapshot_id, wallet_id, asset_id, balance_observation_id, quote_observation_id, included, quality | References exact observations, including carried-forward ones with old times |
| observation_invalidation | id, balance_set_id, detected_at, reason, replacement_set_id | Append-only; excludes invalidated inputs from current state and marks dependent chart points |
| worker_status | worker_id, heartbeat_at, started_at, last_error_code | Background health visible independently of API availability |
| operational_event | id, kind, entity_id, created_at, safe_details | Scope changes, failures, recovery and provider-data removal; no secrets/raw provider bodies |

## Relationships

- Owner controls installation-wide integrations, schedules and wallet membership.
- Wallet/asset pairs yield balance observations; catalog/manual sources only determine scope.
- A snapshot binds a membership revision to exact balance/quote observation references.
- Failed current reads may reuse a previous successful observation only through an
  explicitly stale valuation line. Do not copy an old quantity into a new successful read.
- Quote data may exist without holdings when a token is subsequently sold; source
  provenance allows removal of provider-derived fields on an explicit owner request.
- A quote purge nulls monetary payloads/references and marks affected history unpriced;
  raw observations and timestamps remain. It must not silently report a zero value.

## State transitions and transactions

**Installation**: empty -> key initialized -> migrated -> owner setup -> operational.
Existing DB without its key -> recovery-required. Never manufacture a replacement key.
Owner creation, setup closure and first session occur in one transaction.

**Job**: queued -> running -> completed | partial | retry_wait | failed | cancelled.
Expired lease -> retry_wait, with a new fencing token. Authentication errors mark the
integration needs_attention; budget exhaustion yields waiting_budget until reset/change.
Lease owner must still match at publish time. No database lock spans an external HTTP call.

**Discovery**: queued -> per-wallet scanning -> complete/partial. Its catalog version and
wallet-set revision are frozen. A restart resumes completed wallets; unfinished wallets
whose observation block is unavailable restart with a new safe block and clean cursor.
Finishing a wallet merges nonzero candidates idempotently and requests balance refresh.

**Balance publication**: capture scope revision; obtain safe block; read monitored
pairs, all manual contracts per active wallet and ETH; verify canonical hash; commit
observations atomically after lease/scope validation. A changed scope cancels publication
and queues one new job. A known orphaned block publishes nothing. A successful read
at a later verified block creates a new observation even when raw units are unchanged;
the balance set's block_time is the freshness reference and observed_at is the read time.
Retrying publication of the same observation identity creates no duplicate.

**Valuation publication**: capture membership, latest usable observations and quote set
under a transaction-consistent read; calculate with Decimal; insert summary and lines
atomically by snapshot_key. New quote timestamps create new observed valuations even if
the numerical price is unchanged; retries of the same quote set do not. A new verified
balance set at a later block likewise creates a new valuation even if quantities are
unchanged; retrying the same balance set does not.
Membership changes create labeled new snapshots and never rewrite old membership.

**Reorg**: safe/unfinalized observation -> finalized after canonical hash check, or
invalidated after mismatch. Invalidation propagates to any valuation using those balances.
If historical hash checks are unavailable, keep a verification-pending label.

## Indexes, retention and scale

- Unique identity indexes for wallets, native/contract assets, monitored pairs and inputs.
- Job partial unique index by kind for queued/running/retry_wait/waiting_budget; index on
  state/next_attempt_at/lease_until. Claim short row locks using SKIP LOCKED.
- Balance index(wallet_id,asset_id,observed set order), quote index(asset_id,provider_time),
  snapshot index(created_at,id), membership foreign-key indexes and invalidation set index.
- No zero-balance row per catalog contract in permanent history: discovery stores counts
  and candidates; balance jobs include monitored/manual pairs and ETH, including true zeros.
- Reference fixture: 500 held pairs × 8,760 hourly snapshots ~4.38m valuation lines,
  plus monitored zero pairs. Quotes and balance sets are shared by reference rather than
  copied into every line. Storage sizing must be measured during implementation.
- No automatic deletion of committed financial history. Successful discovery candidates
  can be compacted to monitored pairs and coverage after 30 days; retain run summaries
  and failures for 90 days. This does not remove financial observations.
- Chart responses choose <=2,000 actual observed points and preserve gaps/scope changes.
  Raw rows remain exportable with cursor pagination; do not invent intermediate values.

## Migration and restart persistence

Alembic migrations run once before API/worker readiness. Use transactional migrations
where supported and test failures against committed fixture data. A failed migration
must not mark services ready or trigger destructive automatic schema reset.
Database and key volumes survive ordinary application restarts. Expired worker leases
are recovered without duplicate publication. Missing keys cause a visible unavailable
state rather than silent replacement.

Full backup/restore procedures, separate backup artifacts, clean-instance restoration
and backup-based upgrade rollback are backlog, not release-1 acceptance requirements.
