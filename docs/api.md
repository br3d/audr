# HTTP API reference

The API **as implemented**, at migration head `0016`. Swagger and Redoc are
deliberately disabled (`docs_url=None` in `api/app.py`), so this document is the
discoverable surface.

The *normative* contract this was written against — including routes that were
specified but never built — lives in the internal release-1 Spec Kit artefacts,
which are not part of this repository. Everything that matters about it is
reproduced here: where the two disagree, §6 below names the divergence; each one is encoded as a
strict-xfail test in `backend/tests/integration/test_http_api_contract.py` so it
cannot drift silently.

---

## 1. Conventions

**Base path** is `/api/v1`, same origin as the SPA — in fact the same process,
which serves the SPA bundle too. Health routes live at `/health/*` with no
version prefix. `/api` and `/health` are the two prefixes reserved against the
SPA fallback; nothing else is.

**Auth levels** used in the tables below:

| Level | Requirement |
|---|---|
| *public* | No credentials. |
| *session* | A valid, unrevoked, unexpired `sid` cookie. |
| *session + CSRF* | The above, plus `X-CSRF-Token` matching the session token, plus `Origin` netloc equal to `Host` when `Origin` is present. |
| *session or token* | The `sid` cookie, **or** an `Authorization: Bearer audr_pat_…` API token — GET/HEAD only, and only for routes on the token allow-list (§3, "API tokens"). |

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
| GET | `/api/v1/version` | `{version, commit, built_at}` for the running build. `version` is the semantic version; `commit` and `built_at` are empty outside CI-built images. Unauthenticated on purpose — the deploy health-gate and `scripts/smoke-test.sh` assert on it before any session exists. See `docs/releases.md`. |
| GET | `/api/v1/setup/status` | `{setup_required}`. Exposes no operational secrets. |
| POST | `/api/v1/setup` | Create the singleton owner. Password 12–128 characters. 201 with `csrf_token` and a session cookie; **409** if an owner already exists. |
| POST | `/api/v1/auth/login` | Verify the password, set the `sid` cookie, return `csrf_token`. 429 with `Retry-After: 900` when throttled. |

> `GET /health/ready` is the only trustworthy external health signal, and you
> must assert on the body. The SPA fallback serves `index.html` for anything
> outside `/api` and `/health`, so a bare `GET /` returns 200 whenever the
> process is up — including when the database behind it is unreachable.

---

## 3. Authenticated routes

### Session

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/auth/session` | session | `{authenticated, expires_at, csrf_token}`. |
| POST | `/api/v1/auth/logout` | session + CSRF | Revoke this session, clear the cookie. 204. |
| PATCH | `/api/v1/auth/password` | session + CSRF | Change the password. Revokes **every** session and clears the cookie. 204; 403 if the current password is wrong. |

### API tokens

Read-only Bearer tokens for scripts and LLM tools that should never see the
owner's password or session cookie. A token looks like `audr_pat_<random>`,
is shown in full exactly once at creation, and only its SHA-256 hash is ever
stored.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/tokens` | session | List active (non-revoked) tokens — never the plaintext. |
| POST | `/api/v1/tokens` | session + CSRF | Mint a token: `name`, `expires_days` (default 90), `redact_addresses`. 201 with the plaintext `token`, returned only in this response. |
| DELETE | `/api/v1/tokens/{id}` | session + CSRF | Revoke immediately. 404 if unknown. |

A minted token authenticates exactly like the examples in §5, substituting the
header `Authorization: Bearer audr_pat_...` for the `sid` cookie — with three
restrictions, enforced by a single dependency next to the session checks:

- **GET/HEAD only.** Any other method with a token → `403`, even on a route a
  session could otherwise mutate.
- **Allow-listed routes only.** A token may read `/api/v1/portfolio`,
  `/api/v1/holdings` (including `/wallets/{id}/holdings`), `/api/v1/assets`,
  `/api/v1/history`, `/api/v1/events`, `/api/v1/allowances`, `/api/v1/news`,
  `/api/v1/alerts`, `/api/v1/status`, `/api/v1/version` and `/api/v1/llm/*`.
  Everything else —
  `/integrations`, `/settings*`, `/jobs*`, `/data/*`, `/exports/*`, `/wallets`
  itself (list/detail/label/stop/reactivate) and `/tokens*` — is unreachable:
  `403` if the route is checked against the allow-list, `401` if the route
  only ever looks at the session cookie and never inspects `Authorization` at
  all. Either way, no token reaches an operator surface or a secret.
- **Must be live.** A revoked or expired token is `401`. Each token also has
  its own failed-attempt throttle (database rows, same pattern as login
  attempts) — too many failures in the window is `429`.

**`redact_addresses`** (opt-in, per token): holdings responses show
`"<wallet label> (0x1234…abcd)"` instead of the full wallet address, so a
token can be pasted into a hosted assistant without handing it the owner's
raw addresses. Off by default.

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
| GET | `/api/v1/wallets/{wallet_id}/holdings` | session or token | Latest balances for one wallet. |
| GET | `/api/v1/portfolio/holdings` | session or token | Latest balances across all wallets. |
| GET | `/api/v1/portfolio` | session or token | The full portfolio envelope from the latest published snapshot. |

The `/portfolio` envelope carries `snapshot_id`, `valuation_time`,
`currency: "USD"`, `priced_subtotal_usd`, `total_usd`, `unpriced_asset_count`,
a `quality` flag set (`incomplete`, `stale_balances`, `stale_prices`,
`mixed_observation_times`, `discovery_overdue`, `verification_pending`,
`invalidated`), balance block number and block time, observation times,
coverage, per-holding lines (`holdings`, one row per wallet/asset — unchanged
for the per-wallet metric cards and wallet views) and `allocations`.

The accounting rule: `total_usd` equals the priced subtotal **only** when every
included holding has both a usable quantity and a usable price. Otherwise
`total_usd` is `null` and the subtotal is labelled incomplete.
`stale_contribution_usd` is the portion of the subtotal attributable to
carried-forward balances — a subset, not an addition. Allocation percentages
refer to the included priced subtotal; when that is zero or unknown they are
`null` and the UI draws no slices.

**Allocations** are aggregated **per asset_id**, not per holding —
two wallets holding the same asset collapse into a single row, so an asset
never appears twice. Each row carries `asset_id`, `symbol`, `value_usd`
(summed across contributing wallets, or `null` if the asset is unpriced),
`percentage` (`"0"` for unpriced rows), `quantity` (summed token amount, or
`null` if any contributing line has an unresolvable decimals/amount — never
silently under-reported), `price_usd`, `wallet_count` (distinct wallets
holding the asset), `read_status` (worst of `error` > `stale` > `pending` >
`ok` across contributing lines, using the same staleness rule as
`stale_contribution_usd`), `included` (`false` only when every contributing
line is excluded) and `logo_url`. Unpriced assets are included in
`allocations` (sorted after the priced rows, which is also why `holdings` is
kept unchanged — removing the Holdings page must not make unpriced assets
invisible) and do not distort the priced rows' percentages, which still sum
to ~100%. `unpriced_asset_count` counts distinct unpriced assets, not lines.

### Assets

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/assets` | session or token | List assets. `excluded`, `held`, `cursor`, `limit` (default 50, max 200). |
| POST | `/api/v1/assets/manual` | session + CSRF | Add a manual contract. 201; 409 with `existing_id` on conflict. |
| PATCH | `/api/v1/assets/{asset_id}` | session + CSRF | Update `excluded` or `decimals_override`. |
| GET | `/api/v1/assets/{asset_id}/icon` | session or token | Serve cached icon bytes. Sets its own `Cache-Control`. |
| GET | `/api/v1/assets/{asset_id}/news` | session or token | Cached news for an asset, newest first. `limit`, `offset`. |

The icon route serves **only what is already cached** and returns 404 on a cold
cache — it never fetches upstream inline, so a cold cache cannot slow down a
dashboard render. The frontend falls back to a generated monogram. The cache is
filled by the `asset_icon_refresh` job.

News is a read-only cache populated by `news_refresh`, which requires a
CoinGecko key and returns early without one.

### History

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/history` | session or token | Value history. `period ∈ 24h\|7d\|30d\|90d\|1y\|all` (default `7d`), `cursor`, `limit`. |
| GET | `/api/v1/history/{snapshot_id}` | session or token | The constituent lines of one snapshot, with `observation_id` provenance, ordered by value descending. |

History contains no zero-filled intervals, no synthetic backfill and no PnL
field. Temporal discontinuities are preserved as explicit gap markers rather
than smoothed over.

### Events and allowances

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/events` | session or token | Indexed ERC-20 Transfer events. Filters: `wallet_id`, `event_type ∈ transfer_in\|transfer_out`, `token_address`; `limit`, `offset`. Ordered block desc, log index desc. |
| GET | `/api/v1/allowances` | session or token | Current ERC-20 approvals derived from indexed Approval events. Filters: `wallet_id`, `unlimited_only`; `limit`, `offset`. Flags `is_unlimited`. |

These are implemented extensions beyond the release-1 spec.

### LLM read surface

Stage 2 of [docs/llm-access.md](llm-access.md): a context-window-shaped
projection for an assistant, not a dashboard feed. Computes nothing new and
calls no provider — it re-reads the latest published snapshot, `history_point`,
and the `asset_news`/`news_alert` caches through the same paths as the routes
above.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/llm/context` | session or token | One portfolio snapshot: `total_usd`, priced subtotal, quality flags, top-8 allocations by value, 24h/7d/30d change, the 5 newest headlines, unread alerts, per-subsystem freshness. |
| GET | `/api/v1/llm/asset/{ref}` | session or token | One asset: quantity, value, price, portfolio share, a 30-day value series (downsampled to ≤30 points), its news, its alerts. |

`{ref}` accepts a symbol (case-insensitive), a contract address, or the
asset's UUID — tried in that order. Every response carries `as_of` and a
plain-English `caveats` array (e.g. "3 asset(s) are unpriced, so total_usd is
null, not zero", "this asset is not currently held"); an unpriced holding is
always `null`, never `0`. Both routes also answer `Accept: text/markdown`
with a compact Markdown rendering of the same data, roughly half the token
cost of the JSON. `GET /context` is capped (top-8 allocations, 5 headlines, 5
alerts) to stay near the ~2k-token budget regardless of portfolio size; what
a cap hides is always named in `caveats`, never silently dropped.

### MCP server

Stage 3 of [docs/llm-access.md](llm-access.md): `audr-mcp`, a thin client
over the two routes above (plus holdings, history, news, alerts, and status)
exposed as MCP tools for Claude Desktop and Claude Code. stdio by default —
the token never leaves the machine running it. Setup instructions, the full
tool list, and the opt-in streamable-HTTP transport are in
[docs/mcp.md](mcp.md).

### Integrations

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/integrations` | session | Each provider with derived health, a sanitized host label, the current revision, and the source actually in use. |
| PUT | `/api/v1/integrations/rpc` | session + CSRF | Store an RPC URL and optional headers. Revision-checked; `allow_private_host` is an explicit opt-in. |
| PUT | `/api/v1/integrations/quotes` | session + CSRF | Select a quote provider, with an API key when that provider requires one. Revision-checked. |
| POST | `/api/v1/integrations/{kind}/validate` | session + CSRF | Enqueue `validate_rpc` or `validate_quotes`. Returns `{run_id, coalesced}`. |

`kind` in the validate path is `rpc` or `quotes`; the stored integration kinds
in the database are `rpc` and `coingecko`.

Neither integration has to be configured to work, so a read also reports what is
being used right now: `effective_source` (the keyless public RPC host or the
active price provider's label) and `using_default`. `configured: false` therefore
means "no owner override", not "unavailable".

The quotes entry carries `options`, the providers the backend can actually drive
— currently `coinmarketcap` (keyless, the default) and `coingecko` (requires a
Demo API key) — each with `requires_api_key` and an explanatory `note`. `PUT
/integrations/quotes` accepts only those ids: `coingecko` without a key is 422,
and selecting `coinmarketcap` clears any stored CoinGecko key, which is the one
piece of state that decides which provider the quote job uses.

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
| GET | `/api/v1/status` | session or token | Operational status: database, worker heartbeat, recovery state, catalog, quotes, schedules, version. |

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

Each divergence below is encoded as an `xfail(strict=True)` test, so an
accidental fix shows up in CI just as loudly as a regression.

| ID | Divergence |
|---|---|
| SD-1 | `GET /api/v1/networks` is not implemented (404). With one supported network its content would be a constant. |
| SD-2 | `GET /api/v1/catalog` is not implemented (404). Catalog coverage is therefore not exposed as its own endpoint. |
| SD-3 | Password change is `PATCH /auth/password`; the spec mandates `PUT` (which returns 405). |
| SD-4 | Async triggers (`POST /jobs`, `POST /integrations/{kind}/validate`, `POST /jobs/{id}/cancel`, `POST /data/provider-purge`) return **200**, not the specified 202. |
| SD-8 | `POST /auth/login` returns 422 on a wrong password; the spec mandates 401. |
| SD-9 | `GET /history` returns `{entries}`, not the specified `{items}`. |

(The numbering skips SD-5, which was resolved and is no longer a divergence,
and SD-6/SD-7, which were never used.)

One more gap worth knowing, from
[release-1-coverage.md](release-1-coverage.md): `api/portfolio.py` hardcodes
`read_status: "ok"` for every returned holding, so no code path currently
surfaces a per-holding read failure through this API even though the field
exists.

Routes outside the release-1 spec entirely — `/events`, `/allowances`,
`/assets/{id}/news`, `/assets/{id}/icon` — are implemented extensions and are
not covered by the FR/SC mapping.
