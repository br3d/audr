# HTTP API reference

The API **as implemented**, at migration head `0016`. Swagger and Redoc are
deliberately disabled (`docs_url=None` in `api/app.py`), so this document is the
discoverable surface.

For the *normative* contract this was written against — including routes that
were specified but never built — see
[`specs/001-ethereum-portfolio/contracts/http-api.md`](../specs/001-ethereum-portfolio/contracts/http-api.md).
Where the two disagree, §6 below names the divergence; each one is encoded as a
strict-xfail test in `backend/tests/integration/test_http_api_contract.py` so it
cannot drift silently.

---

## 1. Conventions

**Base path** is `/api/v1`, same origin as the SPA. Health routes live at
`/health/*` with no version prefix. Both are proxied by nginx; nothing else is.

**Auth levels** used in the tables below:

| Level | Requirement |
|---|---|
| *public* | No credentials. |
| *session* | A valid, unrevoked, unexpired `sid` cookie. |
| *session + CSRF* | The above, plus `X-CSRF-Token` matching the session token, plus `Origin` netloc equal to `Host` when `Origin` is present. |

Every mutating route is *session + CSRF*. Obtain the token from the response
body of `/setup`, `/auth/login` or `/auth/session`; it is a double-submit header
token, not a second cookie.

**Values.** Financial quantities, prices, totals and percentages are exact
decimal **strings** or `null` — never JSON numbers. Raw balances are unsigned
decimal integer strings (full uint256 range). Timestamps are RFC3339 UTC.
Identifiers are UUIDs. `chain_id` is always `1`.

**Null is not zero.** An unavailable, invalidated or purged value is `null` with
an accompanying status field. Nothing in this API reports an unknown value as
zero.

**Errors** all use one envelope:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Human-readable, never containing credentials or stack traces",
    "field_errors": null,
    "retryable": false
  },
  "request_id": "0f9c…"
}
```

| Status | Meaning |
|---|---|
| 400 | Malformed request |
| 401 | Unauthenticated |
| 403 | CSRF or origin check failed; wrong current password |
| 404 | Absent |
| 409 | Conflict or revision mismatch |
| 422 | Invalid value |
| 429 | Throttled — carries `Retry-After` |
| 503 | Not ready |

**Headers.** Every response carries `X-Request-Id`. Responses under `/api/` get
`Cache-Control: no-store` unless the route sets its own (the icon route does).

**Pagination** is keyset/cursor-based with stable ordering. Limits vary per
route and are documented inline.

---

## 2. Public routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health/live` | Liveness. Always 200. Hidden from the schema. |
| GET | `/health/ready` | Readiness: database, migration head, master key, worker heartbeat, catalog, quotes. 200 with `"status":"ok"`, or 503 with per-subsystem detail. |
| GET | `/api/v1/setup/status` | `{setup_required}`. Exposes no operational secrets. |
| POST | `/api/v1/setup` | Create the singleton owner. Password 12–128 characters. 201 with `csrf_token` and a session cookie; **409** if an owner already exists. |
| POST | `/api/v1/auth/login` | Verify the password, set the `sid` cookie, return `csrf_token`. 429 with `Retry-After: 900` when throttled. |

> `GET /health/ready` is the only trustworthy external health signal, and you
> must assert on the body. nginx serves `index.html` for anything outside
> `^/(api|health)/`, so a bare `GET /` returns 200 even when the API is dead.

---

## 3. Authenticated routes

### Session

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/auth/session` | session | `{authenticated, expires_at, csrf_token}`. |
| POST | `/api/v1/auth/logout` | session + CSRF | Revoke this session, clear the cookie. 204. |
| PATCH | `/api/v1/auth/password` | session + CSRF | Change the password. Revokes **every** session and clears the cookie. 204; 403 if the current password is wrong. |

### Wallets

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/wallets` | session | List wallets. `cursor`, `limit` (1–100, default 20). |
| POST | `/api/v1/wallets` | session + CSRF | Add a wallet (`address`, `label`). 201. Duplicate address → 409 with the existing id. |
| GET | `/api/v1/wallets/{wallet_id}` | session | One wallet. |
| PATCH | `/api/v1/wallets/{wallet_id}` | session + CSRF | Edit label or status. |
| DELETE | `/api/v1/wallets/{wallet_id}` | session + CSRF | Permanently remove the wallet and its derived records; returns per-table deleted counts. |
| POST | `/api/v1/wallets/{wallet_id}/stop` | session + CSRF | Stop tracking without deleting. |
| POST | `/api/v1/wallets/{wallet_id}/reactivate` | session + CSRF | Resume tracking. |

Addresses are normalised to lowercase for identity and compared
case-insensitively. Tracking an address asserts nothing about controlling it.

### Holdings and portfolio

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/wallets/{wallet_id}/holdings` | session | Latest balances for one wallet. |
| GET | `/api/v1/portfolio/holdings` | session | Latest balances across all wallets. |
| GET | `/api/v1/portfolio` | session | The full portfolio envelope from the latest published snapshot. |

The `/portfolio` envelope carries `snapshot_id`, `valuation_time`,
`currency: "USD"`, `priced_subtotal_usd`, `total_usd`, a `quality` flag set
(`incomplete`, `stale_balances`, `stale_prices`, `mixed_observation_times`,
`discovery_overdue`, `verification_pending`, `invalidated`), balance block
number and block time, observation times, coverage, per-holding lines and
allocations.

The accounting rule: `total_usd` equals the priced subtotal **only** when every
included holding has both a usable quantity and a usable price. Otherwise
`total_usd` is `null` and the subtotal is labelled incomplete.
`stale_contribution_usd` is the portion of the subtotal attributable to
carried-forward balances — a subset, not an addition. Allocation percentages
refer to the included priced subtotal; when that is zero or unknown they are
`null` and the UI draws no slices.

### Assets

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/assets` | session | List assets. `excluded`, `cursor`, `limit` (default 50, max 200). |
| POST | `/api/v1/assets/manual` | session + CSRF | Add a manual contract. 201; 409 with `existing_id` on conflict. |
| PATCH | `/api/v1/assets/{asset_id}` | session + CSRF | Update `excluded` or `decimals_override`. |
| GET | `/api/v1/assets/{asset_id}/icon` | session | Serve cached icon bytes. Sets its own `Cache-Control`. |
| GET | `/api/v1/assets/{asset_id}/news` | session | Cached news for an asset, newest first. `limit`, `offset`. |

The icon route serves **only what is already cached** and returns 404 on a cold
cache — it never fetches upstream inline, so a cold cache cannot slow down a
dashboard render. The frontend falls back to a generated monogram. The cache is
filled by the `asset_icon_refresh` job.

News is a read-only cache populated by `news_refresh`, which requires a
CoinGecko key and returns early without one.

### History

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/history` | session | Value history. `period ∈ 24h\|7d\|30d\|90d\|1y\|all` (default `7d`), `cursor`, `limit`. |
| GET | `/api/v1/history/{snapshot_id}` | session | The constituent lines of one snapshot, with `observation_id` provenance, ordered by value descending. |

History contains no zero-filled intervals, no synthetic backfill and no PnL
field. Temporal discontinuities are preserved as explicit gap markers rather
than smoothed over.

### Events and allowances

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/events` | session | Indexed ERC-20 Transfer events. Filters: `wallet_id`, `event_type ∈ transfer_in\|transfer_out`, `token_address`; `limit`, `offset`. Ordered block desc, log index desc. |
| GET | `/api/v1/allowances` | session | Current ERC-20 approvals derived from indexed Approval events. Filters: `wallet_id`, `unlimited_only`; `limit`, `offset`. Flags `is_unlimited`. |

These are implemented extensions beyond the release-1 spec.

### Integrations

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/integrations` | session | Configured providers with derived health, a sanitized host label, and the current revision. |
| PUT | `/api/v1/integrations/rpc` | session + CSRF | Store an RPC URL and optional headers. Revision-checked; `allow_private_host` is an explicit opt-in. |
| PUT | `/api/v1/integrations/quotes` | session + CSRF | Store the quote provider and API key. Revision-checked. |
| POST | `/api/v1/integrations/{kind}/validate` | session + CSRF | Enqueue `validate_rpc` or `validate_quotes`. Returns `{run_id, coalesced}`. |

`kind` in the validate path is `rpc` or `quotes`; the stored integration kinds
in the database are `rpc` and `coingecko`.

Secrets are never returned. A read exposes only `configured: true` and a
sanitized host label — an RPC URL is not echoed back verbatim, and saving an
unchanged masked field cannot overwrite the stored secret. A `PUT` with a stale
`revision` returns 409.

Validation deliberately does **not** use the public-endpoint failover chain: it
probes exactly the endpoint you configured, so this screen tells the truth about
your own key rather than about a fallback.

### Settings and schedules

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/settings` | session | Current revision and all schedule configurations. No secret fields. |
| PATCH | `/api/v1/settings` | session + CSRF | Bulk schedule patch, revision-checked. 422 on a non-positive interval or freshness. |
| GET | `/api/v1/settings/schedules/{kind}` | session | One schedule. |
| PATCH | `/api/v1/settings/schedules/{kind}` | session + CSRF | Update freshness or budget with `expected_revision`. |
| POST | `/api/v1/settings/schedules/{kind}/pause` | session + CSRF | Pause. |
| POST | `/api/v1/settings/schedules/{kind}/resume` | session + CSRF | Resume. |

Pausing is explicit — a zero interval is not a pause. Changing an interval
returns a usage projection and warnings; actual jobs never exceed the configured
hard budget, and manual triggers share the same budget.

### Jobs and status

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/v1/jobs` | session + CSRF | Trigger a run. `kind ∈ balances\|quotes\|discovery\|valuation`. Enqueues `pending`, coalescing onto any existing pending or in-progress run of that kind. |
| GET | `/api/v1/jobs` | session | Job history, newest first. `cursor`, `limit` (≤100, default 20), optional `kind`. |
| GET | `/api/v1/jobs/{job_id}` | session | One run: state, checkpoints, counts and sanitized errors. |
| POST | `/api/v1/jobs/{job_id}/cancel` | session + CSRF | Request cancellation. The worker stops between bounded batches. |
| GET | `/api/v1/status` | session | Operational status: database, worker heartbeat, recovery state, catalog, quotes, schedules, version. |

Coalescing is why repeatedly mashing refresh produces one scan and one snapshot
rather than several.

### Exports and data lifecycle

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/exports/portfolio` | session | Streamed export. `format=json\|csv`. |
| GET | `/api/v1/exports/history` | session | Streamed export. `format=json\|csv`, `from`, `to`. |
| GET | `/api/v1/data/provider-purge-preview` | session | What a purge would delete. Requires `provider`. |
| POST | `/api/v1/data/provider-purge` | session + CSRF | Irreversible removal of provider-derived data. Requires `confirm: true` and the current password. |

Exports are schema-versioned (`schema_version`, `exported_at`, typed records
with stable ids). They contain **no** passwords, session tokens, RPC URLs,
headers or API keys. Financial values are exact strings in both formats; unknown
and non-applicable fields are null or empty with an explicit status, never zero.
Historical rows retain the metadata revisions that applied when they were
published. CSV text fields are formula-neutralised without altering numeric
source strings. Responses are `Cache-Control: no-store`.

A purge preserves on-chain records, disables the affected quote integration and
fences its active jobs inside the same transaction, so a late provider response
cannot recreate removed data. Re-enabling quotes afterwards is a separate,
explicit owner action. The scope is on-instance payloads — copies already held
by the operator or the provider are outside it.

---

## 4. Rate limiting and throttles

- **Login**: 10 failed attempts in 15 minutes → 429 with `Retry-After: 900`.
  Attempts are database rows, so the cooldown survives a restart.
- **Outbound**: process-wide token buckets for RPC, CoinMarketCap and the
  CoinGecko icon endpoint are shared by every job, so concurrent job kinds
  cannot collectively trip a provider's quota. Defaults are in
  [architecture.md §8](architecture.md#8-configuration-reference).

---

## 5. Worked example

```bash
BASE=http://localhost
JAR=$(mktemp)

# 1. Is this instance claimed?
curl -s "$BASE/api/v1/setup/status"
# {"setup_required":true}

# 2. Claim it. Keep the cookie and the CSRF token.
CSRF=$(curl -s -c "$JAR" -X POST "$BASE/api/v1/setup" \
  -H 'Content-Type: application/json' \
  -d '{"password":"correct horse battery staple"}' | jq -r .csrf_token)

# 3. Add a wallet (mutation → cookie + CSRF header).
curl -s -b "$JAR" -X POST "$BASE/api/v1/wallets" \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" \
  -d '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045","label":"demo"}'

# 4. Trigger a balance scan, then read the portfolio.
curl -s -b "$JAR" -X POST "$BASE/api/v1/jobs" \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" \
  -d '{"kind":"balances"}'
curl -s -b "$JAR" "$BASE/api/v1/portfolio" | jq '{total_usd, quality}'
```

Reads need only the cookie. Mutations need the cookie **and** the header.

---

## 6. Known divergences from the specification

Tracked under AUD-335 and encoded as `xfail(strict=True)` tests, so an
accidental fix shows up in CI just as loudly as a regression.

| ID | Divergence |
|---|---|
| SD-1 | `GET /api/v1/networks` is not implemented (404). With one supported network its content would be a constant. |
| SD-2 | `GET /api/v1/catalog` is not implemented (404). Catalog coverage is therefore not exposed as its own endpoint. |
| SD-3 | Password change is `PATCH /auth/password`; the spec mandates `PUT` (which returns 405). |
| SD-4 | Async triggers (`POST /jobs`, `POST /integrations/{kind}/validate`, `POST /jobs/{id}/cancel`, `POST /data/provider-purge`) return **200**, not the specified 202. |
| SD-5 | Error-envelope shape — **resolved** in AUD-320; no longer xfail. |
| SD-8 | `POST /auth/login` returns 422 on a wrong password; the spec mandates 401. |
| SD-9 | `GET /history` returns `{entries}`, not the specified `{items}`. |

(SD-6 and SD-7 are unused; the numbering skips them.)

One more gap worth knowing, from
[release-1-coverage.md](release-1-coverage.md): `api/portfolio.py` hardcodes
`read_status: "ok"` for every returned holding, so no code path currently
surfaces a per-holding read failure through this API even though the field
exists.

Routes outside the release-1 spec entirely — `/events`, `/allowances`,
`/assets/{id}/news`, `/assets/{id}/icon` — are implemented extensions and are
not covered by the FR/SC mapping.
