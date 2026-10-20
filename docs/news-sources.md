# News collection without paid APIs — the option space

Written for AUD-503. The question from the founder: *collect news about the
crypto projects we hold, with no paid APIs — what are the options, and what do
they cost us?*

Sections 1–8 map the option space, state the pros and cons of each option
honestly, and end with a staged recommendation. They were written before the
scope decision and are kept as-is, because the trade-offs still apply to
anything added later. **§9 records what actually shipped and how to operate
it** — read that first if you are adding coverage for a new holding rather
than re-opening the design question. The classification/alerting half of the
feature is specified separately in ADR-007 (AUD-337).

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
| Release/engineering | `github.com/<org>/<repo>/releases.atom`, `/commits/<branch>.atom`, `/tags.atom` | Ships before the press writes about it — but only for protocol software; see §9 "Release feeds are not automatically signal" |
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

## 9. What shipped, and how to operate it

The founder approved stages 1 and 2. Both are merged and running on staging
with **no API key anywhere in the news path** — the CoinGecko news code is
deleted, not disabled.

| Stage | Issue | What it does |
| --- | --- | --- |
| 1 | AUD-512 | `news_feed` registry + conditional-GET RSS/Atom poller; GDELT breadth tier |
| 2 | AUD-513 | Snapshot governance proposals for held protocols |
| 3 | AUD-515 | Relevance allow-list, GDELT throttling, per-tier commits |

Stage 3 was not in the original plan. It exists because the first staging run
reproduced, in production data, exactly the keyword-ambiguity failure §3
predicted: 44 of 121 ingested rows landed on a meme token ticked `BITCOIN`
whose `asset.name` is `HarryPotterObamaSonic10Inu`, and 4 more on a token
named `ASSET`. Matching headlines against *every held asset* is not viable at
any portfolio size. The fix is the rule §3 had already stated for Snapshot,
applied everywhere: **coverage comes from an explicit mapping, never from
guessing at a symbol.**

AUD-571 then found the inverse gap the scalar `news_feed.asset_symbol`
column created: a feed could only ever map to *one* asset, so ETH — a
top-two holding — got no news at all even though the Ethereum blog and
`go-ethereum/releases.atom` feeds were already being polled, just pinned to
WETH. `news_feed` → asset is now many-to-many (`news_feed_symbol`); adding
ETH to a feed already covering WETH did not require a new feed or any
keyword matching.

### The registries

Everything the job polls lives in a table, not in code, so adding coverage is
an INSERT and needs no redeploy. Seed coverage through a migration anyway
(0024/0025/0026/0031 all do): a hand-run INSERT covers the one install it was
typed against and silently misses a fresh database.

- **`news_feed`** — one row per RSS/Atom URL (`key`, `tier`, `url`,
  `enabled`). Carries `etag`/`last_modified` for conditional GETs and
  `consecutive_failures`/`last_error` for health.
- **`news_feed_symbol`** — many-to-many `(feed_id, asset_symbol)` mapping
  (AUD-571). A feed with one or more rows here is attributed exactly:
  every item is inserted once per held asset whose symbol is mapped to that
  feed (a project blog or a GitHub `releases.atom` can cover more than one
  asset — e.g. `project:ethereum-blog` and `release:go-ethereum` map to both
  `WETH` and `ETH`). A feed with **no** rows here is a trade-press feed,
  matched by keyword against the allow-list below instead. Any `INSERT` into
  this table — the manual path included, not just application code — resets
  the target feed's `etag`/`last_modified` **and `last_polled_at`**
  (migrations 0029/0030, AUD-573): a plain mapping INSERT must not leave the
  feed's next poll 304'd on a body that was already fetched before the new
  symbol existed, and clearing the validators alone is not enough — with a
  live `last_polled_at`, `list_due_feeds` holds the feed back until its
  15-minute window comes round anyway.
- **`snapshot_space`** — one row per Snapshot space (`asset_symbol`,
  `space_id`). Governance proposals are attributed by space, never by symbol.
- **`news_tracked_asset`** — the allow-list of symbols eligible for
  trade-press matching and for a GDELT query. Seeded with the four symbols
  `news_feed` already curates: WETH, UNI, AAVE, LINK. **Being held is not
  enough**; a symbol only becomes eligible when someone opts it in here.
  Migration 0031 extended coverage to the rest of the real portfolio and
  deliberately added **no** rows here — see the preference order below.

#### What is actually covered (migration 0031, AUD-568)

0024 seeded four symbols because those were the ones the test fixtures used;
0031 grew the registries to the holdings that matter. Every source was checked
live before seeding — HTTP 200 plus a parseable body for a feed, a
`proposalsCount` over Snapshot's GraphQL API for a space.

| Asset | Share of portfolio | Sources |
|---|---|---|
| KNC | ~65% | `blog.kyberswap.com` + `KyberNetwork/kyberswap-interface` releases |
| ETH | ~11% | `blog.ethereum.org` + `go-ethereum` releases (shared with WETH, AUD-571), plus `ethresear.ch` + `ethereum-magicians.org` forum feeds (0035) |
| ENS | ~5% | `blog.ens.domains` + `ensdomains/ensjs` releases (0032), plus `ens-contracts` releases + Snapshot `ens.eth` |
| CRV, LDO | dust | `news.curve.finance`; `blog.lido.fi` + Snapshot `lido-snapshot.eth` |
| MATIC | dust | `maticnetwork/bor` releases — retired by 0038, now `forum.polygon.technology` Announcements |
| SUSHI | dust | Snapshot `sushigov.eth` |

Left uncovered on purpose, so a later edit has to argue the case rather than
assume an oversight:

- **FOLD** (~9%) and **OMG** (~5%) — no blog, no releases, and Snapshot spaces
  with 1 and 0 proposals. Holding value does not create a source.
- **Snapshot `curve.eth`** — 203 proposals, newest created 2022-04-29. Outside
  the 90-day retention window, so polling it can only ever insert zero rows.
- **A SUSHI release feed** — both `sushiswap/sushiswap` and
  `sushi-labs/sushiswap` serve a valid but entry-less `releases.atom`.
- **Sub-$1 dust** (ARB, APE, 1INCH, BAL, COW, COMP, DYDX) — Snapshot spaces
  exist; coverage would be completeness for its own sake.
- **`discuss.ens.domains/latest.rss`** — live, and a forum feed is an
  established tier here (`project:aave-governance` is one), but it carries
  per-post traffic rather than announcements: AAVE, the only asset on a forum
  feed, has ~53 rows against everything else's 10–20.

`ens-contracts` releases and Snapshot `ens.eth` are wired but quiet — newest
release 2026-03-13, newest proposal just outside retention. 0031 shipped ENS
on those two alone and the first live run proved the point: every other seeded
asset picked up rows and ENS picked up none, which is why 0032 added the blog
and `ensjs`. Keep them anyway — low-volume is not dead, and a contract release
or a governance proposal outranks a blog post when one lands. The general rule:
distinguish "no rows yet" from "no source", but do not stop at "wired but
quiet" while a live first-party feed is still unlooked-for.

##### Forum feeds, declined for ENS and accepted for ETH (0035, AUD-623)

The forum tier the bullet above declines on volume grounds was accepted for ETH
in 0035, and the difference is the asset, not a change of mind. ETH/WETH is the
largest position, so it is the first card on the page — and its two first-party
sources publish weekly, which is what produced the "news stopped three days
ago" report that opened AUD-623 while AAVE, ENS and UNI all carried rows from
that morning. `ethresear.ch/latest.rss` and `ethereum-magicians.org/latest.rss`
both post daily, map explicitly to `ETH`/`WETH`, and need no API key. The trade
— a card that moves daily with technical forum noise against one that moves
weekly with announcements — was put to the founder and accepted. ENS's card is
fifth and its blog already posts within the window, so the same trade is still
not worth making there.

##### "Wired but quiet" is not the same as "never polled" — LINK

A feed can also sit at `last_polled_at IS NULL` **forever, correctly**, and that
reads like a stall unless you know why. `project:chainlink-blog` and
`release:chainlink` were seeded by 0024 and, as of 2026-10-16, had never been
polled once in the four days since — while every other enabled feed, including
ones created the same morning, carried a `last_polled_at` from the latest run.

They are not broken and not failing. `handle_news_refresh` skips any
project/release feed whose symbols do not intersect the **held** set:

```python
# jobs/news.py
for feed in due_feeds:
    if feed.symbols and not (feed.symbols & held_symbols):
        continue
```

`_get_held_assets` is "at least one `balance_observation.raw_amount > 0`", and
**LINK is not held** — so the two rows are dormant capacity that starts polling
by itself the moment LINK is acquired. Leave them. (LINK's seat in
`news_tracked_asset` is the separate tier-3 allow-list, which is also why it
shows zero rows twice over.)

The diagnostic that settles this class in one pass, rather than re-deriving it:

| `last_polled_at` | `consecutive_failures` | Meaning |
|---|---|---|
| NULL | 0 | **Never attempted** — look at the caller's skip filter, not the feed |
| NULL or stale | > 0 | Attempted and failing — look at the URL and `last_error` |
| fresh | 0 | Polling fine; zero rows is a *source* question ("wired but quiet") |

A NULL poll time with **zero** failures never means a bad URL or a missing
`news_feed_symbol` mapping — both were fine here. It means nothing ever called
it, so the answer is upstream in the job, not in the registry row.

##### Release feeds are not automatically signal (0037, AUD-640)

§2 called the release tier "zero noise". The live data disagreed: the founder
opened AUD-640 on an ENS card showing four `@ensdomains/ensjs@5.2.x` rows in a
week. Counted across the whole release tier at the time of the report:

| Feed | Rows | What they were |
|---|---|---|
| `release:ensjs` | 14 | npm package tags across four packages of one monorepo (`ensjs`, `ensjs-abi`, `ensjs-react`, `ensjs-query-core`) |
| `release:kyberswap-interface` | 11 | front-end app builds, `KyberSwap Interface - v7.11.x` |
| `release:polygon-bor` | 10 | 8 of them `-beta`, `-preconf (Testing Only)`, `-hotfix` tags; 2 stable |
| `release:go-ethereum` | 8 | 4 node releases, each on both ETH and WETH |

Two distinct problems, and they need different fixes — this is the part worth
carrying forward:

1. **A feed that publishes developer artefacts and nothing else** is a registry
   mistake, not a filtering problem. `ensjs` and `kyberswap-interface` have
   never produced a single entry a portfolio owner would want, so a title
   filter would only turn them into two permanently empty polls. 0037 sets
   `enabled = false` on both. The bar for disabling rather than filtering: the
   asset must not lose its last source. ENS keeps `project:ens-blog`,
   `release:ens-contracts` and Snapshot `ens.eth`; KNC keeps
   `project:kyberswap-blog`. Note the irony against 0032, which added `ensjs`
   *because* ENS had zero rows — volume from the wrong repo is not coverage.
2. **A good feed that also publishes pre-release tags** is a filtering problem.
   `polygon-bor` ships genuine node releases interleaved with betas and
   testing-only builds. `is_noisy_release()` in `jobs/news.py` drops, for the
   `release` tier only, titles that carry a package tag (`…@5.2.4`) or a
   pre-release marker (`alpha|beta|rc|nightly|canary|preconf|pre-release|
   unstable|testing only`). A `-hotfix` release is kept: on a node client that
   is news.

Filtered at ingestion rather than in the API or the UI, so a dropped row costs
no retention, no `news_analyze` pass and no card slot. The tier check is what
keeps a *blog* post titled "Our beta is live" — an announcement — while
dropping a *release* tagged `v2.10.2-beta5`.

When adding a release feed, read the repo's last ten release titles first. A
node, client or contracts repo is signal; an SDK, a front-end app or a
monorepo that tags per package is not.

##### …and the tier itself was the mistake (0038, AUD-640 reopened)

The paragraph above is kept because the reasoning is still worth reading, but
its conclusion was wrong. The founder reopened AUD-640 on the MATIC card —
against the rows 0037 had *deliberately kept* as signal:

    v2.10.3 · v2.10.2 · v2.10.1-hotfix2 · v2.10.1-hotfix

Nothing is filterable there. That is simply what a release-tier row looks
like: a `releases.atom` entry has no headline, the title *is* the tag, so the
row renders as a version number next to a source label. Whether the release
is a security fix or a dependency bump is not in the data we store, and a
reader cannot tell. "Protocol software vs. developer artefact" was the wrong
axis; the format was the problem all along.

0038 therefore sets `enabled = false` on the whole tier (`polygon-bor`,
`go-ethereum`, `ens-contracts`, `uniswap-v3-core`, `aave-v3-core`,
`chainlink`) and purges the stored rows. `is_noisy_release()` stays in the
ingestion path as a backstop, and `ck_news_feed_tier` still allows `release`,
so re-enabling is one UPDATE if a future feed earns it.

The disable bar from 0037 — *the asset must not lose its last source* — held
for five of the six. MATIC failed it: `release:polygon-bor` was its only feed,
and an asset with no enabled project/release feed falls through to the GDELT
breadth tier (`covered_asset_symbols`), which for a three-symbol ticker is
close to nothing. So 0038 seeds `project:polygon-forum-announcements`
(`forum.polygon.technology/c/announcements/6.rss`) first.

Picking that URL took five probes; the migration docstring lists them, and
the shape generalises. Polygon has no blog feed at all (`/rss.xml`,
`/blog/rss.xml` → 404 on an Astro site; `blog.polygon.technology/feed/` → 301
to HTML). Discourse always has `.rss`, but **`/latest.rss` is the
unmoderated firehose** — on this forum "Best 10 Real Estate CRM Development
Companies in Columbus" sits between the Heimdall releases, and the same spam
leaks into `/c/polygon-improvement-proposals/80.rss`. Take the moderated
category feed, `/c/<slug>/<id>.rss`, and read its titles before seeding;
`/c/announcements/6.rss` gives sentences ("Bor v2.10.3 Release Mainnet and
Testnet POS", "Polygon PoS RPC Incident — Post-Incident Summary") — the same
events the bor tags were gesturing at, written for a reader. Category ids come
from `/categories.json`.

That category is a *validator announcements* board, so its subject matter is
releases even though its tier is `project` — 1 of its 25 entries is
"Heimdall-v2 v0.12.2-beta-2 (Amoy)", a testnet build post. `should_drop_feed_item`
in `jobs/news.py` therefore applies the pre-release half of the filter to the
feeds named in `_RELEASE_SHAPED_PROJECT_FEEDS`, and the whole filter to the
release tier. Membership is by feed key, not by tier: a general project blog
posting "Our beta is live" is an announcement and must survive. If you seed
another release-centric forum category, add its key to that set.

### Adding a new holding to news coverage

In preference order, best signal first:

1. Does the project publish a feed? Add it to `news_feed` and map the
   asset(s) it covers in `news_feed_symbol` — attribution is then exact and
   no matching is involved. A GitHub repo always has
   `https://github.com/<org>/<repo>/releases.atom`. If an existing feed
   already covers the project under a different symbol (e.g. ETH vs. WETH,
   same underlying blog and release feed), add a second
   `news_feed_symbol` row for the same `feed_id` rather than a duplicate
   feed.
2. Is it a governance token with a Snapshot space? Add the space to
   `snapshot_space`.
3. Only if neither exists, and only if the ticker is distinctive enough to
   survive a headline search, add it to `news_tracked_asset`. Check the ticker
   against `asset.name` first — this is the step that produced the
   `BITCOIN`/`HarryPotterObamaSonic10Inu` mess.

Short, generic or English-word tickers (`T`, `ETH`, `ASSET`, `COW`) belong in
1 or 2, never in 3.

#### Why tier 3 stayed at four symbols (AUD-578)

After 0031/0032 the open question was whether to extend
`news_tracked_asset` past its original WETH/UNI/AAVE/LINK to the rest of the
seeded portfolio (KNC/ENS/LDO/CRV/MATIC/SUSHI/ETH). Measured on the stand
before deciding, and the answer is no — not as a precaution, but because the
volume the extension was supposed to buy is not there:

- **The tier it governs produces ~5% of the corpus.** Of 173 rows in the
  90-day window, 9 came from the press tier (8 Defiant, 1 Cointelegraph) and
  **0 from GDELT** — despite all four tracked symbols clearing
  `_GDELT_MIN_SYMBOL_LEN`. The breadth tier of last resort is, in practice,
  empty.
- **AAVE's lead is the forum feed, not the press.** Its 53 rows are 45 from
  `project:aave-governance` plus 8 press. Attributing that gap to tier 3 was
  the wrong read; the lesson generalises to `news_feed_symbol`, not here.
- **Five of the seven candidates match nothing.** Across a 179-headline
  snapshot of the live press feeds (CoinDesk 308'd; Cointelegraph, Decrypt,
  Defiant, Bitcoin Magazine read), word-boundary matching on symbol-or-name
  scored KNC 0, ENS 0, LDO 0, CRV 0, SUSHI 0, MATIC 1.
- **The one symbol that would gain is the one the rule names.** `ETH` scored
  10/179 (~5.6% of all press headlines) — and matching is on `asset.symbol`
  **or** `asset.name`, so `\bEthereum\b` fires on every "Ethereum, Altcoins
  See Outsized Drawdowns"-class market story. That is not an
  `asset.name` *collision* like `BITCOIN`; it is an over-broad **true** match,
  which the distinctive-name test does not catch. ETH would bury its own 16
  exactly-attributed rows in ecosystem noise within days.

So the failure mode to screen for in step 3 is two-sided: a name that matches
the wrong asset, *and* a name that correctly matches far more than the
holding. Reopening this needs a measurement that beats the one above, not an
argument that the names look distinctive.

### Operational notes

- Retention is 90 days, one constant shared with ADR-007's alert retention.
  Items older than that are dropped before insertion, not after: a feed body
  is the whole history upstream still serves, not a delta, and one live poll
  wrote 1288 rows then pruned 1272 of them in the same run for an effective
  gain of 16 (AUD-575).
- Each tier (feeds, GDELT, Snapshot) commits independently, so a late-tier
  failure cannot roll back successful polls earlier in the same run.
- A failing source degrades to skip-and-log and increments its failure
  counter; it never fails the run. Silent coverage decay is still the dominant
  failure mode here — watch `consecutive_failures`.
- GDELT is rate-limited by design: capped query count, request spacing, and an
  abort after consecutive 429s. It is the breadth tier of last resort, not a
  primary source.
- Rows written before the stage-3 allow-list were misattributed by definition
  and are deleted by migration 0027.
