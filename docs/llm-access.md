# Making audr readable by an LLM (AUD-622)

A design proposal, not an implementation record for stage 4 onward. It
answers one question: what would have to change so that an assistant like
Claude or ChatGPT could answer "how is my portfolio doing, and is there news
I should care about?" against a live audr instance.

**Stages 1–3 are built.** Stage 1 (read-only API tokens) shipped as AUD-629;
stage 2 (the `/api/v1/llm/*` read surface) as AUD-630; stage 3 (`audr-mcp`,
the MCP server — see [docs/mcp.md](mcp.md)) as AUD-632. Stage 4 (curated
OpenAPI document, `/.well-known/llms.txt`) is not yet built. §7 names the
staged work and the order.

---

## 1. Where we stand today

Every read route in [api.md](api.md) is gated on a browser session: a
`Set-Cookie: sid` obtained from `POST /auth/login`, 24-hour expiry, plus an
`X-CSRF-Token` double-submit header on every mutation and an `Origin`/`Host`
match. The SPA is served by the same process on the same origin, which is the
only client that was ever designed for.

That is a good shape for a single-owner self-hosted box and a bad shape for a
machine client:

- **There is no credential a non-browser client can hold.** No bearer tokens, no
  API keys, no scopes. A session cookie cannot be minted out-of-band, cannot be
  scoped to reads, and dies after a day.
- **There is no machine-readable contract.** `docs_url=None` and
  `redoc_url=None` in `api/app.py`; the OpenAPI document is not served at all.
  `docs/api.md` is prose — excellent for a human, unusable as a tool schema.
- **The read surface is shaped for a dashboard, not for a context window.**
  Answering the question above means `GET /portfolio` + `GET /history?period=30d`
  + `GET /alerts` + one `GET /assets/{uuid}/news` per asset of interest — and
  the model does not know our UUIDs, so it cannot even address an asset by
  `ETH`. The `/portfolio` envelope is deliberately verbose (per-wallet lines,
  seven quality flags, provenance on every row); pasted whole it is a
  five-figure token bill for a two-line answer.
- **What does work today, with zero code:** `GET /api/v1/exports/portfolio` and
  `/exports/history` (`format=json|csv`). An owner can download those from the
  browser and paste or attach them to a chat. No news, no alerts, stale the
  moment it is saved, and large histories overflow context — but it is the
  honest answer to "what can I do this afternoon".

---

## 2. Stage 1 — read-only API tokens

The prerequisite for everything else. Without it there is no stage 3, no
ChatGPT action, and no `curl` in a cron job either.

- New table `api_token`: `id`, `name`, `token_hash`, `scopes`, `expires_at`,
  `created_at`, `last_used_at`, `revoked_at`. Store a hash, never the token.
- `Authorization: Bearer audr_pat_<random>` accepted as an alternative to the
  `sid` cookie — **on safe methods only**. The enforcement belongs in one
  dependency (a sibling of `_require_session`) that rejects any non-GET/HEAD
  request from a token with 403, so a route added next year is read-only by
  default rather than by the author remembering. Mutations stay
  session + CSRF forever: an assistant must not be able to trigger a job, write
  an integration secret or run `POST /data/provider-purge`.
- Not every GET is in scope. Tokens see portfolio, holdings, assets, history,
  events, allowances, news, alerts, status, version. They do **not** see
  `/integrations`, `/settings*`, `/jobs*`, `/data/*` or `/exports/*` — those are
  operator surfaces and two of them carry provider budget or destructive
  previews.
- Minting, naming, listing and revoking live on a Settings page; the token is
  displayed once. Per the constitutional rule that routine operation needs no
  shell, this cannot be an env-var or a CLI-only feature. Default expiry 90
  days, renewable.
- Per-token throttle (the existing login-attempt pattern generalises: rows in
  the database, survives restart) and `last_used_at` shown in the UI so a
  forgotten token is visible.
- Optional per-token `redact_addresses` flag: wallet addresses are replaced by
  their labels and a truncated hash before the response leaves the process.
  Pasting a token into a hosted assistant means that data crosses to a vendor;
  this makes the smaller disclosure possible.

Cost: one migration, one dependency, one settings screen, docs. Backend-heavy
with a small frontend slice.

## 3. Stage 2 — a read surface shaped for a context window

A new `/api/v1/llm/*` family. It computes nothing new and calls no provider: it
re-reads the latest published snapshot, the history rows, the news cache and the
alert table through the existing read paths, and projects them small.

| Route | Returns |
|---|---|
| `GET /api/v1/llm/context` | One snapshot of everything: `total_usd`, priced subtotal, quality flags, top-N allocations by value, 24h/7d/30d deltas, the newest headlines, unread alerts, and per-subsystem freshness. Budget: under ~2k tokens. |
| `GET /api/v1/llm/asset/{ref}` | One asset: quantity, value, price, portfolio share, a downsampled 30-day series, its news, its alerts. |

Two properties that matter more than the route list:

- **`{ref}` accepts a symbol or a contract address, not only a UUID.** A model
  has `ETH` and `0xa0b8…`; it does not have our identifiers. Without this every
  question costs an extra listing round trip and invites a hallucinated id.
- **The existing truthfulness rules are kept, and made explicit.** Exact decimal
  strings, never JSON floats. `null` is never zero. Every block carries `as_of`.
  And each response carries a `caveats: []` array in plain English — "balances
  carried forward from block N, 3 hours stale", "two assets unpriced, so
  `total_usd` is null" — because a model that is handed a bare subtotal will
  present it as certain. The array is the cheapest available defence against a
  confident wrong answer about someone's money.

Offer `Accept: text/markdown` alongside JSON. For a conversational client the
Markdown rendering is roughly half the tokens of the equivalent JSON and needs
no tool-side formatting.

Cost: backend-only, no schema change.

## 4. Stage 3 — an MCP server

This is the actual integration point, and it is why the HTTP work above is worth
doing rather than publishing a custom GPT.

A small `audr-mcp` package: a thin client over the stage-2 routes, holding one
bearer token, exposing tools `get_portfolio`, `list_holdings`, `get_asset`,
`get_history`, `list_news`, `list_alerts`, `get_status` and a resource
`audr://portfolio/current`. Every tool wraps a GET; none wraps a POST. Job
triggers are deliberately absent in v1 — they spend a metered provider budget,
and a model should not be the thing that spends it.

Two transports:

- **stdio** — the recommended path. Claude Desktop and Claude Code launch the
  process locally, the token never leaves the operator's machine, and the data
  path is browser-equivalent. This is the one that matches our single-owner
  self-hosted constraint.
- **streamable HTTP** behind the same bearer token, as an opt-in compose service
  (`MCP_ENABLED=false` by default), for hosted connectors.

Why MCP rather than "we already have an API": the tool descriptions and
JSON Schemas *are* the prompt. The model picks the right call and the right
arguments without us maintaining instructions, and the same server works in
Claude Desktop, Claude Code and ChatGPT's connector support. A custom GPT
action, by contrast, is one vendor, needs a publicly reachable host, and gives
us no way to withhold a route.

Cost: a new package and a compose service, both additive. Nothing in the backend
changes.

## 5. Stage 4 — discovery for everything that is not MCP

Cheap, and best done alongside stage 2.

- Serve a **curated OpenAPI document** at `GET /api/v1/openapi.json`, auth-gated,
  containing the token-visible read routes only — not the mutating ones. That is
  precisely what a ChatGPT action, a codegen client or a one-off script wants.
  Keep `docs_url=None`: a schema for authenticated clients is not the same thing
  as a public Swagger page.
- A static, unauthenticated `/.well-known/llms.txt`: what this instance is, that
  it is single-owner and private, where the schema and the MCP endpoint are, and
  how to authenticate. It must contain no portfolio data and no instance
  specifics beyond that — it is reachable by anyone who can reach the host.
- An "automation and LLM access" section in `api.md`, since that document is the
  discoverable contract by design.

---

## 6. The security line, and where it does not move

Portfolio composition plus wallet addresses is the most sensitive data this
system holds; de-anonymising an address set is not hard. So:

- Read-only is structural (one dependency, method-checked), not per-route
  discipline.
- Route allow-list, not deny-list: a new route is invisible to tokens until
  someone adds it.
- Tokens hashed at rest, shown once, revocable individually, expiring by
  default, rate-limited per token, with `last_used_at` surfaced in the UI.
  `Cache-Control: no-store` already covers `/api/`.
- The documentation has to say the uncomfortable part plainly: **a token pasted
  into a hosted assistant hands that vendor your portfolio.** Local stdio MCP is
  the recommended configuration; the hosted-connector transport ships disabled
  and turning it on is an explicit owner decision, as is every token mint.
- No secret ever becomes token-readable: integrations, settings and the purge
  surface stay session-only, which the allow-list enforces.

## 7. Recommendation

Order: **1 → 2 → 4 → 3.** Stage 1 is the only hard dependency; stages 2 and 4
make the surface usable and self-describing; stage 3 is the part the founder
will actually feel, and it is also the part that is pure addition once 1–2 exist.
Stage 1 alone already unlocks scripted reads and a cron'd digest.

Today, with no code: download `GET /api/v1/exports/portfolio?format=json` and
attach it to a chat. It answers the portfolio half of the question and none of
the news half.

## 8. Considered and rejected

| Option | Why not |
|---|---|
| Manual export paste, permanently | Zero cost and it works, but it is stale, manual, and carries no news or alerts. Fine as a stopgap, not as the answer. |
| Reuse the browser session cookie from a script | 24-hour expiry, CSRF on anything that writes, and no way to scope it to reads. Every automation breaks daily. |
| Push a periodic Markdown digest to a file or object store | No interactivity — the model cannot ask a follow-up — and it still needs the digest builder, which is stage 2. Worth revisiting *after* stage 2 as a cheap extra. |
| Embeddings / vector store over our data | Over-engineered and actively harmful here: the data is small, structured and numeric, retrieval by API beats retrieval by similarity, and an embedding of a price is a stale price. |
| A local LLM inside the stack | Out of scope for the single-host, no-GPU deployment constraint. The assistant lives on the operator's side; we supply data. |
