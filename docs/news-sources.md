# News collection without paid APIs — the option space

Written for AUD-503. The question from the founder: *collect news about the
crypto projects we hold, with no paid APIs — what are the options, and what do
they cost us?*

This document maps the option space, states the pros and cons of each option
honestly, and ends with a staged recommendation. It is an options paper, not a
decision: the scope decision belongs to the founder (AUD-503), and the
classification/alerting half of the feature is already specified separately in
ADR-007 (AUD-337).

---

## 1. Where we are today

News already exists in the codebase, in a shape that constrains the answer:

- `asset_news` (migration `0006_asset_news`) stores one row per
  (asset, source, external_id) with `title`, `url`, `news_site`,
  `thumbnail_url`, `published_at`, `fetched_at`, and a uniqueness constraint
  `uq_asset_news_dedup` on `(asset_id, source, external_id)`.
- `audr.jobs.news.handle_news_refresh` is the `news_refresh` worker job
  (schedule fallback 900 s). It pulls CoinGecko's *general* news feed and
  matches articles to held assets by word-boundary keyword match on symbol and
  name, because the Demo tier cannot filter by coin.
- `GET /api/v1/assets/{asset_id}/news` reads the cache. Read-only; the API
  never fetches inline.
- **The current source requires a CoinGecko API key.** Without a key the job
  logs a warning and returns. That is precisely the dependency AUD-503 wants
  removed.

Three consequences for everything below:

1. The storage model is already source-agnostic — `source` + `external_id` is
   exactly what a multi-source ingester needs. **No schema change is required
   to add keyless sources.** Optional additions (`summary`, `lang`,
   `relevance`, `source_feed_id`) are refinements, not blockers.
2. The matching stage is already separate from the fetching stage. A new source
   only has to produce candidate items; `match_assets` already decides which
   held asset an item belongs to.
3. Whatever we add is polled by the existing worker and carries the existing
   self-throttle discipline. No new long-running process is implied by options
   A, B or E.

---

## 2. Option A — public RSS/Atom feeds

Poll the feeds that publishers already publish for exactly this purpose. Three
tiers of feed, in descending signal quality:

| Tier | Examples | Why it matters |
|---|---|---|
| Project-native | `blog.ethereum.org/en/feed.xml`, Mirror (`mirror.xyz/<addr>/feed/atom`), Medium (`medium.com/feed/@org`), protocol blogs | First-party announcements: upgrades, incidents, tokenomics |
| Release/engineering | `github.com/<org>/<repo>/releases.atom`, `/commits/<branch>.atom`, `/tags.atom` | Ships before the press writes about it; zero noise |
| Trade press | CoinDesk, Cointelegraph, Decrypt, The Defiant, Bitcoin Magazine | Breadth, context, market reaction |

**Pros**

- Genuinely free, no key, no account, no quota, no vendor who can revoke us.
  This is the only option with *no* counterparty risk.
- Feeds are a consumption contract: polling them politely is the intended use,
  with none of the ToS ambiguity of scraping.
- Cheap and well-mannered: `ETag`/`If-Modified-Since` conditional GETs make a
  15-minute poll of 50 feeds nearly free in bandwidth and CPU.
- Stable, boring formats. `feedparser` (BSD) or a small `xml.etree` parser over
  `httpx`, which we already depend on.
- Dedupe is solved by the format: feed `id`/`guid` maps straight onto
  `external_id`, so `uq_asset_news_dedup` does the work.
- GitHub release feeds give per-project coverage for assets the trade press
  never writes about — the long tail of a small portfolio.

**Cons**

- **Zero coverage of X/Twitter and Telegram**, where crypto news actually
  breaks first. Expect to be 10–120 minutes behind the timeline.
- Per-project feed curation is manual: someone maps asset → feed URLs. A
  catalog of ~30 held assets is an afternoon; it then rots as projects migrate
  blogs (Medium → Mirror → self-hosted is a common path).
- Feeds carry title + summary, often truncated. Full-article text needs a page
  fetch, which reintroduces scraping questions (see option C).
- Trade-press feeds are portfolio-blind: we fetch everything and discard most
  of it, exactly as the CoinGecko job does today.
- Silent failure is the normal failure: a moved feed 404s or returns an HTML
  error page forever. Needs per-feed health tracking and a visible "stale
  feed" signal, or coverage quietly decays.

**Effort:** one backend issue. Feed registry (seeded table + settings UI
later), conditional-GET poller, Atom/RSS normaliser into the existing
`asset_news` insert path, per-feed health counters.

---

## 3. Option B — protocol-native and governance sources

Treat "news about a project we hold" as what the protocol itself emits, not
what journalists write.

- **Snapshot** — `hub.snapshot.org/graphql`, free, no key: proposals, voting
  windows and outcomes per space.
- **Governance forums** — Aave, Uniswap, Lido, Arbitrum, Compound all run
  Discourse, which exposes `/latest.json`, `/c/<category>.json` and per-category
  RSS without a key.
- **Tally / on-chain governor events** — proposal created/queued/executed, via
  our own RPC and the event indexer we already have (`jobs/event_indexer.py`).
- **GitHub Atom** — overlaps option A's release tier.

**Pros**

- Highest signal-to-noise of any option. A Snapshot proposal on a protocol we
  hold is, by construction, relevant; no keyword matching, no relevance
  scoring, no LLM needed to decide it matters.
- Structured JSON with real identifiers and timestamps — clean `external_id`,
  no HTML, no dedupe heuristics.
- Attaches to assets by address/space, not by symbol. It cannot confuse our UNI
  holding with the word "uni" in an unrelated headline, which is a live failure
  mode of the current keyword matcher.
- For governance-heavy DeFi holdings this is the *earliest* signal available —
  earlier than the press, and often earlier than X.
- Fits the product's stated bias (`product-vision.md`): claims stay linked to
  verifiable sources.

**Cons**

- One adapter per source family, and the work does not amortise: Snapshot,
  Discourse and Tally share no shape. N protocols, N configuration entries.
- Only covers protocols that *have* on-chain governance. ETH, stablecoins,
  wrapped assets, memecoins and most ERC-20s get nothing from this option.
- Discourse JSON endpoints are undocumented-but-public: no stability promise,
  per-IP rate limits, occasional Cloudflare interstitials.
- Forum chatter is high-volume and low-value; without filtering to official
  categories (ARFC/TEMP CHECK and similar) it floods the feed.
- Not "news" in the founder's everyday sense. It answers "what is this protocol
  doing" rather than "what happened to my bag today" — complementary to A, not
  a substitute.

**Effort:** one issue for Snapshot (covers the most ground per line of code),
one for Discourse, one for governor events if we want them. Independently
shippable; each is useful alone.

---

## 4. Option C — search aggregators and scraping

Query a per-asset search and ingest the results: Google News RSS
(`news.google.com/rss/search?q=<query>`), Bing News RSS, or HTML scraping of
aggregators and article pages.

**Pros**

- One adapter covers every asset via a query template — the opposite of option
  B's per-protocol cost. Add an asset, get coverage, no curation.
- Genuinely broad: catches regional press, niche outlets and the long tail that
  curated feeds miss.
- Google News RSS in particular is keyless, returns clean Atom, and answers
  arbitrary queries — the single highest coverage-per-effort source available
  for free.
- Can be added as a *fallback* tier for assets with no native feed, which is a
  real gap in options A and B.

**Cons**

- Google News RSS is undocumented and unsupported. It can change or start
  rejecting datacentre IPs without notice, and it is at minimum in tension with
  Google's ToS. We would be building on something we are not licensed to use.
- SEO spam and syndication duplicates dominate query results for ticker
  symbols. Cross-source dedupe (canonical URL, title shingles) becomes
  mandatory rather than nice-to-have.
- Ticker queries are ambiguous in a way feeds are not ("APE", "UNI", "OP").
  Query construction becomes a tuning project.
- HTML scraping of article pages is fragile, needs `robots.txt` compliance, and
  storing full text raises a republication question we should not take on for a
  single-owner tool.
- From a self-hosted box on a residential IP this mostly works; it is exactly
  the kind of thing that breaks on a VPS behind a flagged ASN, and it breaks
  silently.

**Effort:** small to build (one adapter), ongoing to keep alive. The cost is
maintenance, not construction.

---

## 5. Option D — self-hosted bridges for closed platforms

Get X, Telegram and Discord without paying, by running a bridge ourselves:
RSSHub (Docker), a Nitter instance, `t.me/s/<channel>` HTML, or Telegram
MTProto/Bot API with a free `api_id`.

**Pros**

- The only option that reaches where crypto news actually breaks. Minutes, not
  hours — the difference between a useful alert and a historical record.
- Telegram's APIs are free and legitimate: a free `api_id`, or a bot added to
  public channels. No ToS gymnastics, and `t.me/s/<channel>` is public HTML.
- RSSHub self-hosted converts dozens of closed platforms into the same RSS
  shape option A already consumes, so the ingestion side is reused.
- Highest value for the alerting half of the feature (ADR-007): exploit and
  depeg news surfaces on X first, by a wide margin.

**Cons**

- X is the hard part and the valuable part. Free X access has been
  systematically closed: Nitter is effectively dead, scrapers break weekly,
  and it violates X's ToS. Any X path here is unreliable *and* legally
  uncomfortable — not a foundation.
- RSSHub is another container to run, update and debug, against our
  "small Docker Compose stack" constraint (`containers.md` exists precisely
  because we keep pruning services).
- Telegram ingestion needs a session or bot identity, i.e. a long-lived
  credential and, for MTProto, a phone-bound account that can be banned. New
  class of secret to manage.
- Channel/account quality varies wildly; unsourced claims and paid shilling are
  the norm. Feeding this into recommendations without provenance tracking is a
  product risk, not just an engineering one.
- `roadmap.md` explicitly excludes Telegram news *ingestion* from release 2.
  Choosing this option is a roadmap change, not just a technical one.

**Effort:** Telegram public channels ~one issue. RSSHub ~one infra issue plus
permanent operational surface. X — recommend not attempting for free.

---

## 6. Option E — keyless or free-tier HTTP APIs

Structured APIs that cost nothing. Two sub-classes, and the distinction matters
more than the label:

**E1 — truly keyless.** GDELT 2.0 Doc API (`api.gdeltproject.org/api/v2/doc/doc`):
no key, no account, global news index updated every 15 minutes, JSON out,
keyword and language filters. Wikipedia/Wikidata event streams. Public RSS
endpoints of aggregators.

**E2 — free tier behind a key.** CryptoPanic's free developer plan, CoinGecko
Demo (what we use today), NewsAPI's developer plan, Reddit OAuth, GitHub API.

**Pros**

- Structured, ranked, deduped results for a fraction of option A's curation
  work — one integration, all assets.
- GDELT is a serious asset: no key, no quota paperwork, enormous coverage,
  built for exactly this query shape. It is the best free *breadth* source that
  carries no ToS ambiguity.
- Free tiers (E2) usually come with relevance and sentiment already computed,
  which the LLM stage in ADR-007 would otherwise pay for.
- Reddit OAuth and GitHub API are generous, documented and stable — fine where
  they fit.

**Cons**

- E2 reproduces the exact problem AUD-503 is asking us to remove: a key, a
  quota, a counterparty. CoinGecko already taught us this — the news job is
  dead without a key today. NewsAPI's free plan prohibits production use
  outright. "Free tier" is a price, not an architecture.
- GDELT's crypto coverage skews mainstream and lags specialist outlets; it
  indexes general news, so ticker queries pull in noise and it is weak on
  protocol-level events.
- GDELT has no published rate limit and no SLA. Throttle conservatively and
  treat outages as normal.
- Quota exhaustion fails silently and looks like "no news today" — the same
  failure shape as a stale feed, and it needs the same health signalling.

**Effort:** GDELT ~half an issue (one adapter, keyword query per held asset).
E2 sources: cheap to add, but each one is a new key to store, rotate and
monitor.

---

## 7. Comparison

| | A. RSS/Atom | B. Protocol/gov | C. Search/scrape | D. Closed-platform bridges | E1. GDELT | E2. Free tier + key |
|---|---|---|---|---|---|---|
| Cost | none | none | none | hosting | none | none, but keyed |
| Counterparty risk | none | low | **high** | **high** | low | **high** |
| ToS comfort | clean | clean | **grey** | **grey→bad (X)** | clean | clean |
| Coverage breadth | medium | narrow | **wide** | medium | **wide** | medium |
| Signal/noise | good | **best** | poor | poor | poor | medium |
| Latency | 10–120 min | minutes | 15–60 min | **minutes** | ~15 min | 15–60 min |
| Build effort | medium | medium, N× | low | high | **low** | low |
| Maintenance | medium | low | **high** | **high** | low | medium |
| Fits current schema | yes | yes | yes | yes | yes | yes |

---

## 8. Recommendation

A staged build, in this order. Each stage is independently useful and ships on
its own.

**Stage 1 — keyless core (A + E1).** Feed registry and conditional-GET poller;
seed it with project-native feeds and GitHub release feeds for every held
asset, plus four or five trade-press feeds; add GDELT as the breadth tier for
assets with no native feed. This alone **removes the CoinGecko key from the
news path entirely** — the AUD-503 ask — and gives better per-project coverage
than we have today, because GitHub releases and project blogs beat a general
aggregator for a 30-asset portfolio.

**Stage 2 — Snapshot (B, partial).** Highest signal per line of code, and it
fixes the keyword-ambiguity failure mode for our governance-token holdings by
matching on space/address rather than symbol. Discourse and governor events
only if stage 2 proves the value.

**Stage 3 — reassess.** Decide between C and D *with data*: after stages 1–2
we will know which assets are under-covered, and that answers whether we need
breadth (C) or speed (D). Deciding now is guessing.

**Recommend against** building on scraped X (unreliable and against ToS; the
cost lands on us every time it breaks) and against adding any new E2 key-gated
source as a *primary* dependency, for the reason the current job is broken.

Cross-cutting, regardless of option:

- Keep ingestion in the existing worker; no new always-on process.
- Identify a `User-Agent` honestly, with a contact URL, and honour
  `robots.txt`.
- Store title, summary, link and source only. No full-text republication.
- Per-source health counters, surfaced in the UI. Silent coverage decay is the
  dominant failure mode of every option here.
- Retention: align with ADR-007's 90-day proposal, one policy for news and
  alerts.
- Classification, relevance scoring and alert delivery are ADR-007's (AUD-337)
  scope, not this document's. This paper stops at "the item is in `asset_news`".

---

## 9. Status

Options paper only. Awaiting the founder's scope decision on AUD-503 before any
implementation issue is created.
