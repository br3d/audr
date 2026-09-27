# Provider and Worker Contracts

## RpcReader

Operations: validate_endpoint, resolve_safe_block, read_native, read_token_balances,
read_metadata, canonical_hash and finalized_head. Expose no signing/send-transaction method.
Endpoint config includes encrypted URL/headers, private-host permission and rate settings.

Result item: request_id, wallet identity, asset identity, raw_uint256 or null,
block_number/hash/time, fetched_at, status and sanitized error code.
Error classes: unauthorized, rate_limited, timeout, unavailable, wrong_chain,
unsupported_capability, malformed_response, contract_revert, metadata_conflict,
noncanonical_block. A valid integer zero is success.

Use JSON-RPC 2.0 IDs; handle unordered batch replies, missing/duplicate IDs and per-item
errors. Retry only failed transient items, not already successful items. Reduce an
unsupported batch to smaller chunks and then individual calls. Never multiply retries
across nested adapter and job layers: three attempts per item per job execution.
HTTP batch overhead is counted separately from logical RPC methods.

ERC-20 metadata reads are bounded, untrusted and independent from balance reads.
Missing decimals: use catalog-provenanced fallback or owner override; never assume 18.
Absent decimals on a manual token exposes raw units and blocks valuation only for that asset.
Valid onchain/catalog disagreement sets metadata_conflict pending owner confirmation.

## CatalogSource

Input: bundled Ethereum-only JSON plus manifest with upstream commit, content checksum,
entry count, license and schema version. No runtime fetching from token metadata URLs.
Output: validated unique Ethereum contract identities and metadata provenance.
Native ETH is added by the application, never guessed from an ERC-20 symbol.

Catalog upgrade is transactional, preserves manual assets and monitored pairs and enqueues
new discovery. Reject invalid imports while retaining the previously working catalog.
Coverage states include not_started, in_progress, partial, complete_for_catalog and overdue.
Catalog completeness means all scoped calls finished successfully, not all Ethereum assets.
Excluded assets may remain scanned; exclusion controls valuation, not security judgments.

## PriceProvider

Operations: validate_credentials, quote_many(asset_identities, USD), usage_projection.
Initial adapter: CoinGecko Demo; identity mapping fixed to chain 1 contracts and native ETH.
No wallet address, quantity, label or owner identity is included in quote requests.
The provider still observes server IP and asset interests; disclose this in settings.

Output per asset: decimal_price or null, provider_timestamp, fetched_at, source,
status (available, unpriced, stale, error), error_code. Missing/future-dated (>5min ahead)
or invalid source timestamps cause unknown-freshness/error status, not a fresh quote.
Preserve last successful values separately when a new fetch fails.
Parse JSON numbers as Decimal directly, within research R4 numeric limits.

Batch max 100 token contracts/request plus a separate ETH request when held.
Read x-cg-demo-api-key from encrypted configuration; request include_last_updated_at=true,
precision=full and vs_currencies=usd. Missing assets remain unpriced; no silent fallback.
Accepted quotes are encrypted before persistence with provider attribution/provenance.

Retry only timeouts, network failures, 429 and transient 5xx. Respect Retry-After;
401/403 pauses the integration until revalidated. Invalid data is per-asset failure.
Every attempt reserves local budget before HTTP dispatch. Provider quota/account rules
are external; local tracking includes retries and cannot know use of the key elsewhere.

## Durable jobs

Kinds: validate_rpc, validate_quotes, discovery, balances, quotes, valuation,
canonicality_check and provider_purge. Recurring schedules exist only for discovery,
balances and quotes; other jobs are explicit/derived.
Defaults and UI settings are defined in [HTTP contract](http-api.md).

Claim ready work in a short PostgreSQL transaction with SKIP LOCKED; assign random
lease_token and a 60-second lease. Heartbeat every 15 seconds; extend only if token matches.
Check token and scope revision before publication. Lease loss cancels writes even if
the old network request later finishes. Recovery preserves attempts and validated checkpoints.

One active run per kind; manual refresh returns the active run rather than duplicating it.
Track pending changes that arrive during a run; schedule one follow-up if its frozen scope
would miss a new wallet/manual token. The async worker services short jobs between
discovery chunks; all outgoing RPC calls share the installation token bucket.
Discovery may be long-running and must not monopolize API or quote/balance updates.

Transient retry delays are 1s/4s plus jitter, maximum three attempts/item. Larger
Retry-After values defer work with a visible cooldown. A job execution can resume after
cooldown but retains item attempt counts; exhausted items become partial/failed until a
new scheduled/manual run. Balance runs expire after 10 minutes to avoid unbounded mixed
block reads; discovery restarts an expired/unavailable wallet block and labels partial work.

Pause prevents new scheduled runs but does not kill active work; Cancel stops an active
run after its bounded request/chunk. Cancellation and timeout do not masquerade as success.
After downtime, enqueue at most one missed occurrence per kind, then compute next due
from current time. Default no overlapping catch-up backlog.

Publication uses unique input keys and one transaction for observation/snapshot contents.
No external requests inside DB transactions. Failure counters and run summaries persist.
On graceful shutdown stop new claims, checkpoint discovery and let unfinished leases expire.

## Canonicality and stale data

For balances, resolve safe once; use EIP-1898 if supported. Number-based fallback verifies
the same hash before commit. Recheck non-finalized sets each balance cycle; finalized
sets need no routine historical rereads unless the endpoint reports contradictory history.
On mismatch mark the set and all dependent valuations invalid and refresh current data.
Unavailable verification leaves verification_pending; never invent canonical confirmation.

Partial snapshots may combine successful current reads and last-known observations;
each line retains original block/time and the summary is marked mixed/incomplete.
Quote-only refresh may value previously observed balances, explicitly showing their age.
A discovery success time never substitutes for a balance or quote observation time.
