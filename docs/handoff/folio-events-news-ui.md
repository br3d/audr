# Handoff: Folio Events + News UI sections (AUD-296 block B remainder)

Owner of this document: infraLead. Prepared 2026-09-30 against `origin/main` tip `fca7bfa`.

## Why this document exists

The AUD-296 "Интерфейс" (Folio) epic and its block-B children sat in a **circular block**:
the epic was marked blocked on its children, while children B3/B4 and the news-UI child
AUD-338 each recorded an unblock action of "resume once AUD-296 is unblocked". Nothing in
that cycle was a real dependency. The dependency they all named — the Folio shell from
block A — has been merged for days.

This document records what is actually done and what actually remains, so the remaining
work can start without re-deriving the contract.

## Verified state on `origin/main` (`fca7bfa`)

**Done — the Folio shell (block A):**

- `frontend/src/styles/folio.css` — Folio design system.
- `frontend/src/components/Layout.tsx` — sidebar shell, 10 nav entries, light/dark toggle.
- All 13 pages under `frontend/src/pages/` render inside that shell.

**Done — B5, AI assistant stub (the scope the founder approved: stub now, real LLM later):**

- `frontend/src/components/AssistantPanel.tsx` (134 lines) — chat transcript, input,
  typing indicator, `STUB_RESPONSES` rotation.
- `frontend/src/pages/AssistantPage.tsx`, nav entry at `Layout.tsx:58`.
- Follow-up already marked in code:
  `// TODO(AUD-302): replace stub responses with POST /api/v1/assistant/chat`.
  `ChatMessage` is shaped so wiring the real endpoint needs no UI change.

**Done — the backend for B3 and B4.** Both routers are implemented and registered in
`backend/src/audr/api/app.py:74-75`, under prefix `/api/v1`:

- `backend/src/audr/api/events.py` — `GET /events`, `GET /allowances`
- `backend/src/audr/api/news.py` — `GET /assets/{asset_id}/news`

**Not done — the UI for B3 and B4.** There is no Events page, no News page, no nav entry
for either, and `frontend/src/api/client.ts` has **no** events/news/allowances methods.
This is the entire remaining scope of the epic, and it is frontend-only.

**Deferred by explicit founder decision:** B1, cost basis + P&L (`AUD-296/B1`). It stays in
backlog on purpose and must not be treated as a blocker of the epic.

## Remaining work item 1 — Events section (B3)

Add an Events page to the Folio shell over the two existing endpoints. Both are
session-authenticated, so they go through the usual `client.ts` fetch wrapper (which
already handles CSRF and 401 → `AuthError`).

### `GET /api/v1/events`

Query params — all optional:

| param | type | notes |
|---|---|---|
| `wallet_id` | UUID string | 400 if not a valid UUID |
| `event_type` | `transfer_in` \| `transfer_out` | |
| `token_address` | string | |
| `limit` | int | default 20, `1 <= limit <= 100` |
| `offset` | int | `>= 0` |

Response `EventsResponse`:

```ts
{ total: number, limit: number, offset: number, events: OnchainEvent[] }

OnchainEvent = {
  id: string
  wallet_id: string
  tx_hash: string
  block_number: number
  log_index: number
  event_type: 'transfer_in' | 'transfer_out'
  token_address: string
  from_address: string
  to_address: string
  raw_amount: string   // raw uint256 as a decimal STRING
  indexed_at: string
}
```

Ordering is newest-first (`block_number DESC, log_index DESC`) — preserve it; do not
re-sort client-side.

### `GET /api/v1/allowances` — security signals

Query params: `wallet_id` (UUID), `unlimited_only` (bool, default `false`),
`limit` (default 20, 1–100), `offset` (>= 0).

Response `AllowancesResponse`:

```ts
{ total: number, limit: number, offset: number, allowances: Allowance[] }

Allowance = {
  wallet_id: string
  token_address: string
  spender_address: string
  raw_amount: string        // raw uint256 as a decimal STRING
  is_unlimited: boolean     // >= 2**128 — the "infinite approval" risk signal
  observed_at_block: number
  tx_hash: string
  indexed_at: string
}
```

`is_unlimited` is the security signal the section exists to surface: render those
allowances as a distinct risk state, not as an ordinary row.

### Acceptance criteria (B3)

1. `client.ts` gains `getEvents(params)` and `getAllowances(params)`, typed as above,
   following the existing method conventions in that file.
2. New Events page under `frontend/src/pages/`, mounted in the Folio shell with a nav
   entry in `Layout.tsx` — matching the existing `NAV_ITEMS` shape (page key, label, icon
   from `Icons.tsx`).
3. Events list paginates via `limit`/`offset` against `total`, and filters by wallet and
   event type.
4. Allowances are shown with unlimited approvals visually flagged, plus an
   `unlimited_only` filter.
5. **`raw_amount` is never parsed into a JS `number`.** These are uint256 decimal strings;
   `Number()` silently loses precision above 2^53. Format via the existing
   `MoneyValue`/decimal-string helpers, or render the string as-is.
6. Empty, loading and error states present; `AuthError` handled like other pages.
7. Frontend tests cover: rendering a page of events, the unlimited-allowance flag, and a
   large `raw_amount` rendering without precision loss.

## Remaining work item 2 — News section (B4 backend done, AUD-338 is the UI)

### `GET /api/v1/assets/{asset_id}/news`

`asset_id` must be a UUID (400 otherwise); 404 if the asset does not exist.
Query params: `limit` (default 20, 1–100 — 400 outside), `offset` (>= 0 — 400 otherwise).

Response `AssetNewsResponse`:

```ts
{ asset_id: string, total: number, limit: number, offset: number, news: AssetNewsItem[] }

AssetNewsItem = {
  id: string
  source: string
  title: string
  url: string
  news_site: string
  thumbnail_url: string | null
  published_at: string
  fetched_at: string
}
```

Newest first.

### Acceptance criteria (AUD-338 / B4 UI)

1. `client.ts` gains `getAssetNews(assetId, params)`, typed as above.
2. News surfaces **per asset** — the endpoint is asset-scoped, so it belongs on the asset
   detail view and/or a News section that takes an asset selection. There is no
   all-assets news endpoint; do not invent one client-side by fanning out over every
   asset.
3. `thumbnail_url` is nullable — render a placeholder, never a broken image.
4. External `url` links open safely (`target="_blank"`, `rel="noopener noreferrer"`).
5. Pagination via `limit`/`offset` against `total`; empty/loading/error states present.
6. Tests cover: a rendered news list, the null-thumbnail path, and the empty state.

## Notes for whoever picks this up

- Branch off `main`, run the frontend test suite, then merge to `main` yourself — this team
  does not use pull requests (see `docs/engineering-workflow.md`).
- Be aware that a push to `main` triggers the guarded auto-deploy to the production host,
  so merge only green work.
- Backend tests are unaffected by this work; there is no migration involved.
