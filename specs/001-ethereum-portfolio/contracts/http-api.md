# HTTP API Contract — v1

Base path /api/v1, same origin as the SPA. JSON request/response except export streams.
This is the normative route/behavior contract; FastAPI will generate OpenAPI from the
implementation, checked against this document in contract tests.

## Shared types and rules

- UUID identifiers; RFC3339 UTC timestamps; chain_id is integer 1.
- Addresses are validated Ethereum hex, normalized for identity; checksum display.
  Any valid public mainnet address may be tracked without proof of control; a tracked
  address is not represented as owned or controlled by the application owner.
- Raw balances are unsigned decimal integer strings. Prices, quantities, totals and
  percentages are decimal strings or null, never JSON numbers.
- Common response metadata: request_id, generated_at. Collection responses contain
  items and next_cursor; limit defaults 50, maximum 200, stable ordering by ID/time.
- Errors: {error:{code,message,field_errors,retryable},request_id}. No provider URLs,
  credentials, stack traces or raw upstream error bodies in messages.
- HTTP 400 malformed request; 401 unauthenticated; 403 CSRF/origin denied; 404 absent;
  409 conflict/revision mismatch; 422 invalid value/capability; 429 throttled with
  Retry-After; 503 unavailable. Async work is 202 with run_id and coalesced boolean.
- PATCH settings/integrations requires revision. Stale revisions return 409.
- Cookie authentication on every route except explicitly public setup-status/login/health.
  Mutations require same-origin and CSRF validation. Login/setup enforce origin checks
  before a session exists; setup cannot run once owner exists.
- Password: 12–128 characters. No mandatory uppercase/digit composition.
- Reject more than five failed login attempts per IP in five minutes and twenty per
  account in fifteen minutes; cooldown 15 minutes, persistent across process restart.
  Return a generic error; successful login clears account failures, not unrelated IP bans.

## Routes

| Method and route | Input | Output / behavior |
|------------------|-------|-------------------|
| GET /setup/status | None | {setup_required}; no operational secrets |
| POST /setup | {password} | 201 owner_created, csrf_token and session cookie; 409 if claimed |
| POST /auth/login | {password} | 200 csrf_token and rotated opaque session cookie |
| GET /auth/session | Cookie | {authenticated:true,expires_at,csrf_token} or 401 |
| POST /auth/logout | CSRF | 204; revoke session and expire cookie |
| PUT /auth/password | {current_password,new_password} | 204; revoke every session, require login |
| GET /networks | None | [{chain_id:1,name:"Ethereum",native_symbol:"ETH"}] |
| GET /integrations | None | Redacted config, provider, revision, enabled, health, budgets |
| PUT /integrations/rpc | {revision,url,headers?,allow_private_host?} | Encrypted replacement; mark unvalidated, queue capability check |
| PUT /integrations/quotes | {revision,provider:"coingecko-demo",api_key,enabled} | Encrypted replacement, preserve old observations |
| POST /integrations/{kind}/validate | kind rpc or quotes | 202 validation job; sanitized outcome in job/status |
| GET /settings | None | Schedule/limit/freshness settings with revision; no secret fields |
| PATCH /settings | {revision,schedules?,limits?,freshness?} | Validated updated settings and projected request usage |
| GET /wallets | cursor,limit | Active/inactive wallets with labels and coverage summary |
| POST /wallets | {address,label?,chain_id:1} | 201 wallet, queues ETH/manual refresh and catalog discovery; duplicate returns 409 and existing ID |
| PATCH /wallets/{id} | {label?,tracking_active?} | Updated wallet, membership revision and queued recomputation |
| DELETE /wallets/{id} | CSRF | Permanently removes the wallet and its derived records (observations, coverage, monitored pairs, on-chain events, valuation lines, emptied snapshots/history points); returns per-table deleted counts; unknown id returns 404 |
| GET /assets | cursor,limit,excluded? | Native/catalog/manual/discovered assets, metadata/provenance |
| POST /assets/manual | {contract_address,decimals_override?,symbol_override?} | 201 or existing asset, metadata check; merge duplicate identity |
| PATCH /assets/{id} | {excluded?,decimals_override?,confirm_metadata_override?} | Exclusion/metadata revision; old snapshots unchanged |
| GET /catalog | None | Source, pinned version/hash, count, bundled time, coverage explanation |
| POST /jobs | {kind:"balances"|"discovery"|"quotes"} | 202 run_id; coalesce same-kind active work; enforce budgets |
| GET /jobs | cursor,limit,kind? | Sanitized run/progress/retry history |
| GET /jobs/{id} | None | Run state, checkpoints, attempted/success/failed counts and safe errors |
| POST /jobs/{id}/cancel | CSRF | 202 cancel_requested; worker stops between bounded batches |
| GET /portfolio | wallet_id? | Current quantities, valuation and allocation envelope below |
| GET /history | range:"24h"|"7d"|"30d"|"all",cursor?,limit? | Observed chart summaries, gaps, scope markers, provenance |
| GET /history/{snapshot_id} | cursor,limit | Snapshot and paged exact constituent observations |
| GET /exports/portfolio | format:"json"|"csv" | Authenticated private streamed holdings/settings export |
| GET /exports/history | format:"json"|"csv",from?,to? | Authenticated private stream of recorded observations/valuations |
| POST /data/provider-purge | {provider,confirm:true,current_password} | 202 irreversible provider-data removal; preserves onchain records, updates affected valuations |
| GET /status | None | DB, worker heartbeat, integrations, schedules, version and recovery status |

Provider purge is an explicit data lifecycle operation, not automatic reaction to a failed
request. Require a preview count from GET /data/provider-purge-preview?provider=... and
show the impact before submitting. This route is not a prerequisite for enabling quotes.
It covers on-instance provider payloads; operator-held copies remain outside the application's deletion scope.
The purge transaction disables that quote integration and fences/cancels its active jobs
before deleting payloads, so a late response cannot recreate removed data. Re-enabling
quotes requires a separate owner action after completion.

Public /health/live reports process liveness; /health/ready exposes only ready/unready
status (200/503), not DB credentials or provider details. Worker failure is reported by
authenticated /status; API may serve stored views while worker is degraded.

## Portfolio and history envelopes

Portfolio fields: snapshot_id, membership_revision, valuation_time, currency="USD",
priced_subtotal_usd, total_usd, quality, balance_block, balance_block_time,
balance_observed_at, price_observed_from/to, discovery_completed_at, coverage,
holdings, allocations.

- `priced_subtotal_usd` sums included holdings within the tracked scope that have both a
  usable balance and a usable price, including carried-forward last-success balances.
  `total_usd` equals that subtotal only when every included holding has both usable
  inputs; otherwise `total_usd` is null and the subtotal is labeled incomplete. Before
  any successful observation, no-wallet and never-scanned states do not assert a zero
  total. When every active wallet has a successful balance observation, every included
  holding has a known quantity and every included nonzero holding has a usable price,
  a zero included sum yields `total_usd="0"` and `priced_subtotal_usd="0"` even if
  excluded assets have nonzero holdings. The UI labels an excluded-only portfolio;
  unknown included contributions still yield a null total.
- quality contains incomplete, stale_balances, stale_prices, mixed_observation_times,
  discovery_overdue, verification_pending and invalidated flags.
- Last-known priced quantities MUST contribute once to the subtotal when current reads
  fail. `stale_contribution_usd` is the subset of that subtotal attributable to
  carried-forward balances, not an additional amount. Expose each affected observation's
  last-success time; label a non-null `total_usd` estimated whenever it uses stale
  balances or stale usable prices. Failed reads expose read_status and last_success
  separately; balance and quote freshness are independent.
- Balance freshness is measured from the last successful verified block time, including
  a later block whose balance is numerically unchanged. `balance_observed_at` is the
  separate read time. A failed read retains the previous successful block time and does
  not refresh balance freshness. Repeating the same observation does not add history.
- Each holding identifies wallet_id, asset_id, address/native identity, raw_balance,
  decimals, quantity, price_usd, value_usd, included, metadata_source, read_status,
  block time, read time and last-success times.
- Excluded assets do not contribute; response explains valuation scope.
- Allocation percentages refer to included priced subtotal; if zero or unknown,
  percentages are null and the UI shows no fabricated pie slices.
- A complete positive-value release-1 network allocation is Ethereum 100%.
- Quote attribution and discovery/catalog explanation accompany the data.

History points contain snapshot_id, observed_at, total/subtotal, quality and scope markers.
No zero-filled intervals, synthetic backfill or PnL field. By default return at most 2,000
actual observations per chart window; if gap/marker boundaries exceed that, return a
continuation cursor. Every discontinuity is preserved. Paged exports expose underlying
data without chart sampling.

## Export streams — schema version 1

Both authenticated export routes produce a versioned JSON stream with `schema_version=1`,
`exported_at` and a `records` array, or a CSV stream with one fixed header containing
`schema_version`, `exported_at`, `record_type` and the union of the fields below. Each
record carries its type and stable IDs. JSON financial values are exact decimal strings or null, never
JSON numbers. CSV financial values use the same exact strings; null and non-applicable
fields are empty, with explicit status/quality fields distinguishing unknown from zero.
Timestamps are RFC3339 UTC. Labels and metadata are untrusted text.

| Record type | Required fields |
|-------------|-----------------|
| `wallet` | `wallet_id`, `address`, `label`, `tracking_active` |
| `asset` | `asset_id`, `chain_id`, `kind`, `contract_address` (null for native ETH), `symbol`, `decimals`, `excluded_current`, `metadata_source` |
| `asset_metadata_revision` | `metadata_revision_id`, `asset_id`, `decimals`, `symbol`, `name`, `source`, `observed_at`, `owner_confirmed` |
| `balance_set` | `balance_set_id`, `job_run_id`, `scope_revision`, `block_number`, `block_hash`, `block_time`, `observed_at`, `status` |
| `balance_observation` | `balance_observation_id`, `balance_set_id`, `wallet_id`, `asset_id`, `metadata_revision_id`, `read_status`, `raw_balance`, `quantity`, `block_number`, `block_hash`, `block_time`, `observed_at`, `last_success_block_time` |
| `quote_set` | `quote_set_id`, `job_run_id`, `provider`, `fetched_at`, `status` |
| `quote_observation` | `quote_observation_id`, `quote_set_id`, `asset_id`, `provider`, `quote_status`, `price_usd`, `provider_time`, `fetched_at`; exported `quote_status` can be `purged` after explicit removal |
| `discovery_coverage` | `wallet_id`, `catalog_version_id`, `run_id`, `started_at`, `completed_at`, `attempted`, `succeeded`, `failed`, `remaining`, `status` |
| `membership_revision` | `revision_id`, `created_at`, `reason`, `active_wallet_ids`, `excluded_asset_ids` |
| `valuation_snapshot` | `snapshot_id`, `membership_revision`, `balance_set_id`, `quote_set_id`, `valuation_time`, `total_usd`, `priced_subtotal_usd`, `quality`, `stale_contribution_usd`, `coverage_status` |
| `valuation_line` | `snapshot_id`, `wallet_id`, `asset_id`, `balance_observation_id`, `quote_observation_id`, `included`, `value_usd`, `quality` |
| `observation_invalidation` | `invalidation_id`, `balance_set_id`, `detected_at`, `reason`, `replacement_set_id` |
| `schedule` | `kind`, `enabled`, `interval_seconds`, `freshness_seconds`, `next_due_at`, non-secret budget limits |

`GET /exports/portfolio` contains current wallet/asset identities and their metadata
revisions, latest balance/quote sets and observations, discovery coverage, current
membership/exclusion revision, current valuation and lines, and non-secret schedules.
`GET /exports/history` contains wallet/asset identities, referenced metadata revisions,
all committed historical balance/quote sets and observations, coverage records,
membership/exclusion revisions,
valuations, lines and invalidations in the selected time range, without chart sampling.
Observations, quotes and membership revisions
referenced by selected valuations are included even if created before the requested
range. Historical records retain the revisions and source times that applied when
published. `tracking_active` and `excluded_current` are null on historical wallet and
asset rows respectively; historical inclusion is determined by `membership_revision`
and `valuation_line` records.
The two revision ID lists are JSON arrays of UUID strings, encoded as JSON text in CSV.
Current exclusions never rewrite earlier rows.
An unavailable, invalidated or purged monetary value is null/empty with its status,
never zero. Neither format contains passwords, session tokens, RPC URLs or headers,
quote API keys, or other credentials. Private export responses use `Cache-Control:
no-store`; CSV text fields receive formula neutralization without changing numeric
source strings.

## Settings validation and secret handling

All three schedules support enabled, interval_seconds and next_due_at.
Minimums: balances/quotes 60 seconds; discovery 900 seconds.
Defaults: balances/quotes 900 seconds; discovery 86400 seconds.
Intervals must be integers <=31536000 seconds; pause is explicit, not zero interval.
Freshness defaults: max(300,2×interval) for balance/quotes; 2×interval for discovery.
Allow owner-set freshness thresholds >=60 seconds independently of pausing.

RPC batch size 1–100, parallel chunks 1–4, logical rate 1–1000 calls/s (provider budget
still applies); initial values 50/2/20. Request timeout 5–60 seconds, default 15.
Quote monthly local attempt budget starts at 8,000; quote minute ceiling 30. Changing
an interval returns a usage projection and warnings, but actual jobs never exceed the
configured hard budget. Manual jobs share budgets. No automatic upgrade/payment.

Saving an unchanged masked field cannot overwrite its secret. Secret reads expose only
configured=true and a sanitized host label; an RPC URL is not returned verbatim.
RPC target validation allows owner-confirmed private nodes while denying link-local,
cloud metadata, multicast and redirects; every resolved destination is checked.
Quote provider origin is fixed by its adapter; no arbitrary public proxy endpoint.
Private data routes return Cache-Control: no-store. CSV exports neutralize spreadsheet
formula prefixes in untrusted metadata without altering exact numeric source strings.
