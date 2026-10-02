# Release 1 requirement coverage (AUD-111)

Recorded: 2026-10-02 against `main` (commit `f480223`).

This document maps every functional requirement (FR-001–FR-024) and success
criterion (SC-001–SC-008) in [`specs/001-ethereum-portfolio/spec.md`](../specs/001-ethereum-portfolio/spec.md)
to the automated test(s) that verify it, or records explicitly that nothing
does. It complements, rather than duplicates, [`docs/verification.md`](verification.md)
(one-command test invocations, actual pass/fail run log, the SC-001 timed
walkthrough) and [`docs/verification-history.md`](verification-history.md)
(detailed history/reorg test narrative).

No test anywhere references `FR-0XX`/`SC-00X` identifiers directly, so this
table was built by reading each requirement's exact wording in the spec and
matching it to test behavior topically, confirming by reading the test bodies
rather than inferring from filenames alone.

## How to read "Status"

- **Covered** — the requirement's behavior has a direct, specific automated test.
- **Partial** — some of the requirement is tested; the gap is named in the row.
- **Not covered** — no automated test exercises this requirement; verified only
  narratively (a doc) or not at all.
- **Known spec-drift (SD-n, AUD-335)** — the requirement's literal wording
  currently conflicts with the implementation on purpose; the conflict is
  tracked as an `xfail(strict=True)` test in
  `backend/tests/integration/test_http_api_contract.py` and ruled on
  separately in AUD-335. This is a tracked divergence, not a coverage gap.

## Known spec-drift reference (AUD-335)

`test_http_api_contract.py` encodes these divergences as deliberately failing
(`xfail(strict=True)`) tests so that any accidental fix or regression shows up
in CI:

| ID | Divergence |
|----|------------|
| SD-1 | `GET /api/v1/networks` not implemented (404) |
| SD-2 | `GET /api/v1/catalog` not implemented (404) |
| SD-3 | Spec mandates `PUT /auth/password`; implementation uses `PATCH` (`PUT` → 405) |
| SD-4 | Spec says `202` for `POST /jobs`, `POST /integrations/{kind}/validate`, `POST /jobs/{id}/cancel`, `POST /data/provider-purge`; implementation returns `200` |
| SD-5 | Error envelope shape — **resolved** in AUD-320; no longer `xfail` |
| SD-8 | `POST /auth/login` returns `422` on a wrong password; spec mandates `401` |
| SD-9 | `GET /history` returns `{entries}`, not `{items}`, per spec |

(SD-6 and SD-7 are not used — the file's numbering skips them.)

## FR-001 – FR-024

| ID | Requirement (paraphrase) | Test(s) | Status |
|----|---------------------------|---------|--------|
| FR-001 | Single owner; one-time password setup; no replacement | `test_auth.py::test_concurrent_setup_only_one_succeeds`, `::test_second_setup_returns_409`, `::test_setup_status_no_owner`, `::test_setup_status_owner_exists`; `test_http_api_contract.py::test_setup_creates_owner_201`, `::test_setup_409_when_already_claimed` | Covered |
| FR-002 | Auth required; sign-out; change password; brute-force/plaintext protection | `test_auth.py::test_session_endpoint_requires_auth`, `::test_logout_revokes_session`, `::test_change_password_succeeds`, `::test_change_password_wrong_current_returns_403`, `::test_change_password_invalidates_sessions`, `::test_throttle_activates_after_max_failures`, `::test_throttle_response_includes_retry_after`, `::test_throttle_resets_after_window`; `unit/test_crypto.py::TestSecretRedaction` | Covered |
| FR-003 | Configure/validate/replace RPC + quote credentials via web UI; secrets masked after save | `test_integrations.py::test_put_rpc_saves_url`, `::test_put_quotes_saves_credentials`, `::test_validate_rpc_queues_job`, `::test_put_rpc_invalid_url_returns_422`, `::test_put_rpc_revision_conflict_returns_409`; e2e `rpc.spec.ts` ("RPC form discloses data sharing"), `valuation.spec.ts` ("API key field is a password input") | Partial — masking is inferred from a `host_label`-only response field; no test directly asserts the saved secret value is absent from the PUT/GET response body |
| FR-004 | Mainnet-only; other networks rejected | `contract/test_rpc_reader.py::test_wrong_chain_id_raises`, `::test_correct_chain_id_passes`; e2e `rpc.spec.ts` (Ethereum-only context copy) | Partial — chain-ID rejection is unit/contract-tested on `RpcReader` only; no integration test wires `PUT /integrations/rpc` + validate with a wrong-chain mock through to an owner-visible rejection |
| FR-005 | Add/label/list/stop wallets; case-insensitive uniqueness; no ownership proof required | `test_wallets.py::test_add_wallet_returns_201`, `::test_add_wallet_normalises_address_to_lowercase`, `::test_add_duplicate_wallet_returns_409`, `::test_list_wallets_*`, `::test_patch_wallet_label`, `::test_stop_wallet`, `::test_reactivate_wallet`; `test_holdings.py::test_address_case_deduplication`; e2e `rpc.spec.ts` (no-ownership-proof wording, valid address add) | Covered |
| FR-006 | Native/ERC-20 balance retrieval via owner RPC, no indexer required | `contract/test_rpc_reader.py::test_get_eth_balance_returns_wei`, `::test_erc20_balance_call`; `test_discovery.py::test_discover_persist_scan_shows_holdings` | Covered |
| FR-007 | Catalog + manual discovery, deduplicated, UI states scope/limits | `test_catalog.py::test_import_catalog_reads_vendored_tokens`, `::test_discovery_finds_candidates_from_real_vendored_catalog`; `test_discovery.py::test_catalog_discovery_finds_known_tokens`, `::test_manual_discovery_uses_provided_addresses`, `::test_duplicate_contract_deduplication`, `::test_discovery_gives_full_catalog_coverage_to_every_wallet`; e2e `rpc.spec.ts` ("discover tokens" scope copy) | Partial — discovery/dedup logic is solidly covered at integration level; the owner-facing "scope is catalog + manual only" copy is asserted only by a locally-run e2e test (e2e is not part of CI — see Dependencies below); `test_http_api_contract.py::test_catalog_returns_200` is itself `xfail` (SD-2) |
| FR-008 | Manual contract addition; validation/read failures; chain+contract identity | `test_assets_endpoints.py::test_add_manual_asset_returns_201`, `::test_add_manual_asset_with_overrides`, `::test_add_manual_asset_normalises_address`, `::test_add_manual_asset_duplicate_returns_409` | Partial — "see validation/read failures" is not tested: `backend/src/audr/api/portfolio.py` hardcodes `read_status="ok"` for every returned holding (no code path, and therefore no test, produces any other read status for a contract that fails metadata reads) |
| FR-009 | Expose discovery coverage/progress/per-wallet success-failure; failed reads never become zero | `test_holdings.py::test_unscanned_token_is_unknown_not_zero`, `::test_per_item_failure_does_not_block_others`; `test_discovery.py::test_discovery_gives_full_catalog_coverage_to_every_wallet` | Partial — "unknown vs. zero" and per-item isolation are covered; no test asserts a dedicated per-wallet success/failure status surfaced through the owner-facing API |
| FR-010 | Exact quantities; decimal math; aggregate once per asset; unknown ≠ zero | `unit/test_money.py::TestRawToQuantity`, `::TestQuantityToUsd`, `::TestFormatDecimal`; `test_uint256_balances.py` (both tests); `test_holdings.py::test_exact_raw_unit_stored_and_retrieved`, `::test_zero_balance_is_stored_accurately` | Partial — exact-quantity/decimal math is thoroughly covered; "aggregate assets across wallets once" has no dedicated two-wallet-same-asset test at the portfolio/API layer |
| FR-011 | Dashboard: USD total, asset allocation, network allocation, per-wallet holdings | `test_portfolio_endpoints.py::test_portfolio_returns_snapshot_with_holdings`, `::test_portfolio_wallet_id_filter`; `frontend/src/test/DashboardPage.test.tsx`, `AllocationTable.test.tsx` | Partial — USD total, asset allocation and per-wallet holdings are covered; no API field or test exists for a distinct "network allocation" view (trivially true with one network, but untested as such) |
| FR-012 | Prices tied to asset/source/time; unpriced assets stay visible | `test_valuation.py::test_publish_snapshot_unknown_price_is_null`; `test_quote_price_availability.py::test_quote_refresh_flags_unresolved_asset`, `::test_quote_refresh_clears_flag_once_resolved`; `test_portfolio_endpoints.py::test_portfolio_total_usd_null_when_missing_price` | Covered |
| FR-013 | Stale/partial/complete labeling; carried-forward balances; estimated totals; balance vs. quote freshness | `test_valuation.py::TestComputeQuality` (complete/partial/stale/gaps/mixed), `::test_empty_complete_set_does_not_shadow_prior_prices`, `::test_stale_balance_uses_latest_observation`; `test_portfolio_endpoints.py::test_portfolio_partial_quality_sets_incomplete_flag`, `::test_portfolio_stale_quality_sets_stale_prices_flag`, `::test_portfolio_gaps_quality_yields_total_from_priced_holdings`; `test_quote_refresh_freshness.py` (all 3); `frontend/src/test/DashboardPage.test.tsx` (stale/incomplete/estimated labels) | Covered — the most thoroughly tested requirement in the spec |
| FR-014 | Exclude/reinclude assets; excluded view; no silent snapshot rewrite | `test_assets_endpoints.py::test_list_assets_excluded_filter`, `::test_patch_asset_excluded`; `test_valuation.py::test_publish_snapshot_excluded_asset_omitted` | Covered |
| FR-015 | Successful observations persist as dated history from tracking start | `test_history.py::test_published_snapshot_rows_are_not_modified`, `::test_valuation_lines_are_immutable_after_publication`, `::test_materialize_history_point_idempotent` | Covered |
| FR-016 | History views for 24h/7d/30d/all; gaps/partial/membership visible; never "PnL" | `test_history_api.py::test_history_accepts_all_known_periods[24h/7d/30d/all]`, `::test_history_rejects_unknown_period`; `test_history.py::test_period_24h_excludes_older_points`, `::test_gap_markers_injected_for_temporal_discontinuities`; `frontend/src/test/HistoryChart.test.tsx`, `HistoryTable.test.tsx` (no-PnL wording); e2e `history.spec.ts` (range selector + no-PnL wording) | Partial — periods, gaps and no-PnL wording are covered; "portfolio membership changes MUST be visible" in history has no test, despite `history_point` carrying `included_wallet_count`/`included_asset_count` |
| FR-017 | Configure refresh intervals; pause/resume; manual trigger; reject invalid intervals | `test_schedules.py::test_schedule_validation_rejects_negative_freshness`, `::test_schedule_validation_rejects_zero_budget`, `::test_schedule_pause_and_resume`; `test_settings_patch.py::test_patch_settings_rejects_non_positive_interval_seconds`, `::test_patch_settings_rejects_non_positive_freshness_seconds`, `::test_patch_settings_accepts_positive_interval_seconds`; `test_job_trigger_dispatch.py`; e2e `operations.spec.ts` | Covered |
| FR-018 | Show current work/last attempt/success/next execution/failures; bounded recovery | `test_job_retry_backoff.py` (all); `contract/test_rpc_failover.py` (all 7); `contract/test_rpc_rate_limiting.py` (all 5); `test_worker_leases.py::test_heartbeat_extends_lease`, `::test_worker_lease_recovered_after_crash`; e2e `operations.spec.ts` (StatusPage) | Covered |
| FR-019 | No duplicate/inflated snapshots from repeated or overlapping work; reorgs corrected | `test_job_trigger_dispatch.py::test_trigger_job_coalesces_onto_existing_pending_run`, `::test_trigger_job_enqueues_pending_not_in_progress`; `test_worker_leases.py::test_duplicate_claim_rejected`; `test_reorg.py` (11 tests — see `docs/verification-history.md`) | Covered |
| FR-020 | Config + history survive restart; UI export excludes secrets, retains exact values/provenance | `test_exports.py::test_export_exact_value_preservation`, `::test_export_unknown_not_zero`, `::test_export_csv_unknown_not_zero`, `::test_export_excluded_asset_omitted`, `::test_export_historical_metadata_revision`, `::test_export_full_history_all_snapshots`; `test_migrations.py` (readiness) | Partial — export content/exactness is strongly covered, but no test actually asserts that secrets/credential fields are absent from an export; **restart persistence of wallets/settings/history is not tested anywhere** — `test_restart.py`'s 7 tests cover only master-key persistence, migration-readiness reporting and worker-lease crash recovery, none of which stores a wallet/setting/snapshot and re-reads it after a simulated restart |
| FR-021 | English-only UI; USD currency; responsive/keyboard/chart-text-alternative | `frontend/src/test/HistoryChart.test.tsx` (accessible data table via `details`/`summary`); `AllocationTable.test.tsx` (aria-label, `scope="col"`); e2e `operations.spec.ts`, `history.spec.ts` (390px/1440px + keyboard navigation) | Partial — see SC-008; only 2 of the 4 user journeys have e2e coverage at both required viewport widths |
| FR-022 | Deployable via Docker Compose; no file/terminal edits for routine settings | — | Not covered — no automated test exercises a Docker Compose deployment; verified only by the manual walkthrough in `docs/verification.md` (`docker compose up -d`) |
| FR-023 | Never request private keys/seed phrases/signatures/spending permissions/tx execution | `contract/test_rpc_reader.py::test_reader_has_no_signing_methods` | Covered |
| FR-024 | External requests via configured connections with disclosure; credentials redacted from logs; no telemetry without consent | `unit/test_rpc_targets.py` (SSRF/private-host validation); `unit/test_crypto.py::TestSecretRedaction`; e2e `rpc.spec.ts`, `valuation.spec.ts` (data-sharing disclosure copy) | Partial — credential-at-rest protection and SSRF guarding are covered; no test captures logs and asserts credentials are redacted from them, and "no telemetry without explicit configuration" has no dedicated test |

## SC-001 – SC-008

| ID | Requirement (paraphrase) | Test(s) | Status |
|----|---------------------------|---------|--------|
| SC-001 | Setup → two wallets saved within 5 minutes | None automated; manual timed walkthrough in `docs/verification.md` ("Owner walkthrough — SC-001 timing", ~2m50s excluding image pull, recorded 2026-09-27) | Not covered by automation — verified only narratively |
| SC-002 | Fixtures (0/6/18 decimals, duplicate symbols, tiny/large quantities, repeated addresses) → exact totals to the cent | `unit/test_money.py::TestRawToQuantity` (0/6/18 decimals, 1-wei fractional precision, large uint256), `::TestQuantityToUsd::test_exact_no_float`; `test_uint256_balances.py` (both tests); `test_holdings.py::test_address_case_deduplication` | Partial — no single fixture combines all the named dimensions as SC-002 literally describes; coverage is assembled from several narrower tests, and "duplicate symbols distinguished by contract/network" specifically has no test |
| SC-003 | Every failed/unpriced holding visibly flagged; stale wallet shows last-known quantity + timestamp; never-successful wallet → incomplete | `test_valuation.py::TestComputeQuality` (`test_no_prices_stale`, `test_some_unpriced_partial`); `test_portfolio_endpoints.py::test_portfolio_stale_quality_sets_stale_prices_flag`, `::test_portfolio_partial_quality_sets_incomplete_flag`; `frontend/src/test/DashboardPage.test.tsx` (stale contribution note) | Partial — the invariant is tested piecewise; no single fixture reproduces the exact two-wallet scenario (one stale, one never-successful) in one test, and there is no outage-sweep test proving zero cases of a silent fresh-zero |
| SC-004 | 50 wallets / 100 assets / 1yr hourly snapshots; dashboard+history load ≤3s p95 in ≥95/100 trials | `backend/tests/fixtures/history_scale.py` generates the reference dataset but is not wired into an automated, gating test on `main`. The `benchmark` service/profile described in `docs/verification.md` corresponds to an older placeholder (`pytest --benchmark-only` with no `benchmark` marker) that was removed in AUD-329 | Not covered on `main` — matches the AUD-108 dependency named in this issue; **note:** at the time of writing, a `scripts/benchmark.py` script implementing exactly this p95 gate was present uncommitted in the shared working tree (apparently in-progress AUD-108 work by another agent), but it is not yet part of any `main` commit and is not referenced here as coverage until it lands |
| SC-005 | Schedule changes survive restart; paused work doesn't run; repeated manual triggers → one scan, no duplicate snapshot | `test_schedules.py::test_schedule_pause_and_resume`, `::test_schedule_cooldown_prevents_immediate_retrigger`; `test_job_trigger_dispatch.py::test_trigger_job_coalesces_onto_existing_pending_run` | Partial — pause/resume and de-duplication are covered; the restart-survival half is untested, same gap as FR-020 |
| SC-006 | Restart preserves wallets/settings/snapshots; exports reconcile with history | `test_restart.py` (master-key persistence, migration readiness, lease recovery only); `test_migrations.py` (schema-level readiness) | Not covered as stated — confirmed by direct reading of `test_restart.py`: none of its 7 tests touch the `wallet`, `settings` or `valuation_snapshot` tables; the "restart" simulated there is limited to the master key and worker-lease fencing |
| SC-007 | Unauthenticated reads blocked; no credential leakage in logs/secret views/exports | `test_auth.py::test_session_endpoint_requires_auth`; `test_http_api_contract.py` (401 envelope shape); the many `*_requires_session`/`*_401_unauthenticated` tests across `test_wallets.py`, `test_assets_endpoints.py`, `test_integrations.py`, `test_portfolio_endpoints.py` | Partial — unauthenticated-access blocking is exhaustively covered; "exports/secret views expose no credentials" has no direct test (same gap as FR-003/FR-020/FR-024) |
| SC-008 | All 4 user journeys usable at 390px and 1440px; keyboard-reachable; chart-as-text | e2e `operations.spec.ts` (Schedules/Status/Account & Data, both viewports); `history.spec.ts` (390px); `frontend/src/test/HistoryChart.test.tsx` (accessible data table) | Partial — Journey 4 (control/recover) and most of Journey 3 (history) are tested at both widths; Journey 1 (`auth.spec.ts` setup/sign-in), the wallet-add flow in `rpc.spec.ts`, and Journey 2 (`valuation.spec.ts` dashboard) run only at Playwright's default viewport, with no 390px/1440px variants |

## Known limitations — catalog (FR-007, FR-008, FR-009)

- `test_http_api_contract.py::test_catalog_returns_200` is `xfail(strict=True)` — **SD-2 (AUD-335)**: `GET /api/v1/catalog` is not implemented, so there is no API-level contract test exposing catalog coverage to the owner.
- `backend/src/audr/api/portfolio.py` hardcodes `read_status="ok"` for every returned holding — no code path (and so no test) produces an "unsupported"/"error" read status for a manually added contract that fails metadata reads (FR-008's "see validation/read failures").
- Dedup and full-coverage logic is well tested (`test_discovery.py::test_duplicate_contract_deduplication`, `::test_discovery_gives_full_catalog_coverage_to_every_wallet`), but the owner-facing "coverage is limited to catalog + manual additions" copy is verified only by a Playwright test that is not run in CI (see Dependencies below).
- No test distinguishes duplicate-symbol assets by chain/contract identity in a UI-facing assertion; asset uniqueness is tested only via address+chain uniqueness on the manual-add endpoint.

## Known limitations — pricing (FR-012, FR-013, SC-003)

- `test_valuation.py::TestComputeQuality` directly encodes the gaps-vs-partial-vs-stale distinction with six explicit cases and is the strongest-tested requirement in the spec.
- `test_quote_refresh_freshness.py` and `test_quote_status_staleness.py` cover freshness-driven refetch logic and the `/health/ready` degraded-staleness signal precisely.
- Gap: SC-003's "zero cases where an unavailable value is silently displayed as a fresh zero" is an aspirational sweep claim with no fuzz/outage-matrix test; coverage is case-by-case.
- Gap: the exact SC-003 two-wallet fixture (one failed-refresh wallet showing stale contribution + timestamp, one never-successful wallet showing incomplete-estimate) is not assembled as a single test; its pieces live separately across `test_portfolio_endpoints.py` and `DashboardPage.test.tsx`.

## Known limitations — RPC (FR-004, FR-006, FR-019)

- `contract/test_rpc_failover.py` (7 tests) and `test_rpc_rate_limiting.py` (5 tests) give solid contract-level coverage of failover, sticky-endpoint behavior, retry-budget exhaustion and rate-limiter semantics.
- `test_reorg.py` (11 tests) covers invalidation-record insertion, idempotency, canonicality flips and the `recheck_canonicality` sweep.
- Gap: no test combines an actual RPC failover/rate-limit event with a subsequent scan/valuation run to confirm the owner-visible effect end to end — the contract tests exercise the reader/client in isolation.
- Gap: FR-004's chain-ID rejection is unit-tested on `RpcReader` only; no integration test wires a wrong-chain RPC mock through `PUT /integrations/rpc` + validate to an owner-visible rejection.

## Known limitations — stale-data handling (FR-013, SC-003)

- `test_quote_status_staleness.py` covers the "fresh-looking but actually stalled" scenario (a lingering non-NULL price with a dead refresh pipeline) precisely.
- `test_quote_refresh_freshness.py` and `test_valuation.py::test_stale_balance_uses_latest_observation` jointly cover "balance and quote freshness MUST remain separate."
- Gap: the backend has no single end-to-end test for "the total MUST equal the priced subtotal only when every included holding has a usable quantity and price" — it's inferred from `TestComputeQuality` unit tests plus separate portfolio-endpoint tests, not one scenario.
- Gap: FR-016's "portfolio membership changes MUST be visible" in history has no test coverage at all, despite `history_point` carrying the fields (`included_wallet_count`, `included_asset_count`) that would support it.

## Dependencies and open gaps (AUD-106, AUD-108, AUD-109)

This issue depends on closing AUD-106 (e2e run), AUD-108 (benchmark) and AUD-109
(security audit); rows above reflect the gaps those issues are meant to close.

- **E2E (AUD-106):** `ci/gitea-overlay/workflows/ci.yaml` runs only the `backend-tests` and `frontend-tests` jobs. The Playwright specs in `tests/e2e/*.spec.ts` are not run in CI — `compose.test.yaml`'s comments confirm the containerised `e2e`/`api-test` profiles were removed in AUD-329 (no working `Dockerfile.e2e`), and that the specs are meant to run locally via `cd frontend && npm run e2e`. `docs/verification.md` (2026-09-27) records e2e as "not run" and there is no later verification record of a clean e2e run against `main`.
- **Benchmark (AUD-108):** confirmed not automated on `main` — see SC-004 above. `backend/tests/fixtures/history_scale.py` on `main` is a standalone generator script with no pytest marker and no gating assertion.
- **Security audit (AUD-109):** no dedicated security-audit test suite or CI step exists on `main` (no `security` pytest marker, no SAST/dependency-audit CI step, no `docs/security-*.md`). The closest automated coverage is `unit/test_rpc_targets.py` (SSRF/private-host validation) and `unit/test_crypto.py` (secret redaction), both ordinary unit tests rather than a dedicated audit artifact.

As any of AUD-106/108/109 close, the corresponding rows above (FR-007's e2e-only
UI-copy test, SC-004, and the credential/log-redaction gaps) should be updated
from "not covered"/"partial" to "covered" with the new test references.

## Out of scope for this table

`test_allowances.py`, `test_news.py`, `EventsPage.test.tsx`, `NewsFeed.test.tsx`
and `AssistantPanel.test.tsx` test features (allowance risk detection, news
feed, AI assistant) that the spec's Assumptions section explicitly excludes
from release 1. They are implemented extensions in the current codebase but
are not mapped to any FR/SC above.
