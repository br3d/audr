# Feature Specification: Ethereum Portfolio

**Feature Branch**: Not created; this workspace has no project-local Git repository.

**Created**: 2026-09-25

**Status**: Tasks generated; implementation pending.

**Input**: Build a standalone single-owner Ethereum portfolio with public wallet scanning,
native ETH and ERC-20 holdings, USD valuation, allocation and history from onboarding.
Provide English web-based setup and settings; support owner-selected RPC access without
a mandatory proprietary indexer. News, AI recommendations and alerts are later releases.

## Clarifications

### Session 2026-09-25

- Q: What automatic token discovery is required in release 1? → A: Option A:
  scan a maintained known-token catalog through RPC and support manual contract additions.
  Historical transfer-log discovery is outside release 1.
- Q: How should the total behave when some wallets fail to refresh? → A: Include
  their last successful observations in an estimated total, explicitly identify the
  stale contribution and show when those observations last succeeded. If no successful
  observation exists, keep that contribution unknown and label the estimate incomplete.
- Q: How should release-1 backup and restore be managed? → A: Defer full backup/restore
  to the backlog. Keep ordinary restart persistence and web data export in release 1.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Set up and inspect wallet holdings (Priority: P1)

The owner creates a password, configures Ethereum access, adds labeled public addresses
and sees ETH and supported ERC-20 quantities across those wallets. Tracking an address
does not establish that the owner controls it.

**Why this priority**: Accurate consolidated holdings are the foundation of the product.

**Independent Test**: On a fresh installation, configure a controlled data source and
add two fixture wallets; inspect their holdings without pricing or AI configured.

**Acceptance Scenarios**:

1. **Given** a fresh installation, **When** the owner completes initial password setup,
   **Then** setup closes and protected portfolio/settings views require authentication.
2. **Given** an authenticated owner, **When** valid Ethereum mainnet access and two
   distinct wallet addresses are configured, **Then** scans expose the ETH and supported
   ERC-20 holdings for each wallet and aggregate quantities for matching assets.
3. **Given** an existing address, **When** the same address with different letter casing
   is submitted, **Then** it is recognized as the same wallet and never double-counted.
4. **Given** an invalid address or connection to another network, **When** saved,
   **Then** validation reports the problem and does not start a mainnet scan against it.
5. **Given** a token held by a tracked address outside automatic coverage, **When** its contract is added
   manually, **Then** a valid balance is tracked or an explicit unsupported/error state
   explains why it cannot be read; unrelated holdings remain available.
6. **Given** a previously tracked wallet, **When** the owner stops tracking it,
   **Then** it leaves future scans and current totals while past snapshots remain intact
   and the portfolio membership change is identified in history.
7. **Given** a catalog containing held and unheld tokens, **When** a scan completes,
   **Then** nonzero balances of supported catalog contracts are discovered through the
   configured RPC connection without an indexer account; zero balances are not holdings.
8. **Given** a held contract absent from the catalog and manual additions, **When** a
   scan completes, **Then** the UI states that coverage is limited to the catalog and
   manually added contracts and offers manual addition without claiming universal discovery.
9. **Given** a contract present in both the catalog and manual additions, **When** a
   scan completes, **Then** it contributes once per wallet to the portfolio.
10. **Given** any valid public Ethereum mainnet address, **When** the owner adds it,
    **Then** tracking begins without proof of address control and the UI does not claim
    that the owner controls the address.

---

### User Story 2 - Understand value and allocation (Priority: P1)

The owner sees the portfolio's USD value, asset allocation, wallet details and the
Ethereum network allocation, with visibility into pricing and scan completeness.

**Why this priority**: Consolidated quantities must become an understandable financial view.

**Independent Test**: Use saved fixture holdings and controlled USD prices to compare
the displayed portfolio, asset and wallet totals against independently calculated values.

**Acceptance Scenarios**:

1. **Given** complete holdings and prices, **When** the dashboard is opened,
   **Then** totals equal the sum of included holdings and allocation uses that same total.
2. **Given** a token without a USD price, **When** the dashboard is opened,
   **Then** its quantity remains visible with an unknown value and the priced subtotal
   is explicitly labeled incomplete.
3. **Given** a failed refresh after a successful scan, **When** holdings are displayed,
   **Then** last-known quantities retain their timestamps and stale status instead of
   becoming zero, and contribute to an explicitly estimated total. The stale contribution
   and its last successful observation times are visible separately. Available quotes
   retain their own timestamps; a fresh price does not make an old quantity fresh.
4. **Given** unwanted tokens, **When** the owner excludes one from portfolio valuation,
   **Then** it remains accessible in an excluded-assets view and current totals explain
   that exclusion; past recorded snapshots are not silently recalculated.
5. **Given** no wallets or no nonzero holdings, **When** the dashboard opens,
   **Then** it shows an explanatory empty state without a fabricated allocation chart.
6. **Given** a wallet has never been scanned successfully, **When** its refresh fails,
   **Then** its contribution remains unknown and the portfolio estimate is labeled
   incomplete; the system neither substitutes zero nor claims a complete total.
7. **Given** successful observations and only excluded nonzero holdings, **When** the
   dashboard is opened, **Then** the included total is zero with an explanation that
   holdings are excluded and no allocation chart; an unknown included contribution
   instead keeps the total unknown and labeled incomplete.

---

### User Story 3 - Observe portfolio history (Priority: P2)

The owner sees how observed USD portfolio value changes after tracking starts.

**Why this priority**: History delivers useful perspective without full accounting.

**Independent Test**: Record successive fixture observations, change portfolio membership
and restart the installation; inspect the preserved chart and its coverage labels.

**Acceptance Scenarios**:

1. **Given** two successful observations with known prices, **When** history is viewed,
   **Then** the chart displays their actual observation times and recorded USD values.
2. **Given** an interval before tracking or during an outage, **When** that interval is
   selected, **Then** the view identifies absent/stale observations without inventing values.
3. **Given** a repeated result or a restart, **When** work resumes,
   **Then** history is preserved and retrying an observation does not create a duplicate.
4. **Given** a wallet addition/removal or exclusion change, **When** history is viewed,
   **Then** the change is labeled and is not described as investment profit or loss.

---

### User Story 4 - Control and recover operation (Priority: P2)

The owner manages connections, credentials, refresh frequency and data through the browser.

**Why this priority**: The installation must remain usable without editing application files.

**Independent Test**: Change schedules, trigger scans, simulate provider outages, export
records and restart the application while retaining its persistent storage.

**Acceptance Scenarios**:

1. **Given** configured polling, **When** the owner changes its interval or pauses it,
   **Then** the change persists and the next execution/pause state is visible.
2. **Given** a scan is running, **When** manual refresh is requested repeatedly,
   **Then** the UI reports active work without creating duplicate concurrent scans.
3. **Given** rate limiting or connection failure, **When** a scheduled scan runs,
   **Then** failure and retry state are visible and the owner can still inspect stored data.
4. **Given** configured secrets, **When** settings or normal exports are viewed,
   **Then** secret values are not revealed; replacing a credential remains possible.
5. **Given** committed wallets, settings and history, **When** the application restarts
   with its persistent storage intact, **Then** those records remain available without
   duplicate observations.

### Edge Cases

- Zero ETH with nonzero tokens; tokens with different decimal precision; tiny quantities
  and large integers: preserve quantities and avoid misleading display rounding.
- A successfully read quantity unchanged at a later verified block: refresh its balance
  observation and freshness without duplicating a retry of the same observation.
- Duplicate symbols or misleading metadata: distinguish assets by network/contract and
  render metadata as untrusted text.
- Missing metadata, reverted balance calls or unsupported contracts: mark the affected
  holding unsupported/unknown and continue other reads.
- Partial discovery or a limited catalog: expose coverage
  and never label a wallet completely scanned beyond the actual supported scope.
- Missing or stale prices: show quantities and a labeled partial/stale valuation.
- Same-address duplicates, repeated refreshes and overlapping jobs: do not double-count.
- Network reorganizations: superseded observations must be identifiable and corrected;
  orphaned observations must not remain presented as current confirmed balances.
- Adding/removing wallets or excluding assets: annotate scope changes in future history.
- All known nonzero holdings excluded: distinguish a verified zero included total from
  an unknown total caused by an included holding that has never been observed.
- Interrupted persistence or process restart: retain previously committed data
  and report unfinished work; do not fabricate successful observations.
- Concurrent first-time setup attempts: exactly one owner is created.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST support one owner and create the owner's password once
  during initial setup; subsequent setup attempts MUST NOT replace the owner.
- **FR-002**: Portfolio data and settings MUST require authentication. The owner MUST
  be able to sign out and change the password; credential storage and failed-login
  protection MUST prevent plaintext password exposure and unrestricted guessing.
- **FR-003**: The owner MUST configure, validate and replace Ethereum access and quote
  credentials through the web interface. Secret fields MUST be masked after saving.
- **FR-004**: Ethereum mainnet MUST be the only supported network in release 1.
  Connections reporting a different network MUST be rejected.
- **FR-005**: The owner MUST add, label, list and stop tracking public wallet addresses;
  address uniqueness MUST be case-insensitive. Any valid public Ethereum mainnet address
  MAY be tracked without proof of control; tracking MUST NOT imply ownership.
- **FR-006**: Scanning MUST retrieve native ETH and supported ERC-20 balances using
  owner-selected standard Ethereum RPC without a required proprietary indexer account.
- **FR-007**: Automatic token discovery MUST check a maintained catalog of known
  Ethereum ERC-20 contracts through the configured RPC connection and include supported
  nonzero holdings. The scan scope MUST also include manually added contracts, deduplicated
  by network and contract address. The UI MUST identify catalog/manual coverage and
  explain that tokens outside this scope require manual addition. Historical transfer-log
  discovery and a proprietary indexer dependency are outside release 1.
- **FR-008**: The owner MUST be able to add an ERC-20 contract manually and see
  validation/read failures. Assets MUST use chain and contract identity, never symbol alone.
- **FR-009**: Scans MUST expose their supported discovery coverage, progress and
  per-wallet success/failure; failed reads MUST NOT replace successful balances with zero.
- **FR-010**: The system MUST preserve exact quantities and decimal monetary calculations,
  aggregate assets across wallets once and distinguish unknown quantities from zero.
- **FR-011**: The dashboard MUST show USD portfolio valuation, included-asset allocation,
  network allocation and per-wallet holdings. Ethereum accounts for the sole network share.
- **FR-012**: Prices MUST be associated with the correct asset, source and observation
  time. Assets without prices MUST remain visible with unknown USD value.
- **FR-013**: Priced subtotals and mixed-age/partial valuations MUST carry explicit
  completeness and freshness labels; allocation MUST state its valuation basis.
  Within the included, tracked scope, the priced subtotal MUST include every holding
  with a usable quantity and price, including last successfully observed quantities
  carried forward after a failed refresh. The stale-balance contribution MUST be
  identified as a subset of that subtotal, with each affected observation's last-success
  time; it MUST NOT be added to the subtotal a second time. The total MUST equal the
  priced subtotal only when every included holding has a usable quantity and price;
  otherwise the total MUST be unknown and the priced subtotal labeled incomplete.
  A complete-input total containing carried-forward balances or stale usable prices
  MUST be labeled estimated, never fresh. Balance and quote freshness MUST remain
  separate. Balance freshness MUST use the verified block's time; the time the read
  completed MUST be available separately. A successful read at a later verified block
  MUST establish a new observation and last-success time even if the raw quantity is
  unchanged; a retry of the same observation MUST NOT duplicate history. Contributions
  without any successful balance or usable price MUST remain unknown, never zero.
  No-wallet or never-successfully-scanned states MUST NOT assert a
  zero total. Once every active wallet has a successful balance observation, if every
  included holding has a known quantity and every included nonzero quantity has a usable
  price, a zero included sum MUST have a zero total and no allocation chart. If excluded
  holdings account for all known nonzero holdings, the UI MUST explain that state and
  keep them accessible. Any unknown included contribution MUST keep the total unknown.
- **FR-014**: The owner MUST exclude/reinclude unwanted assets in current valuation and
  inspect excluded assets. Exclusions MUST NOT silently rewrite past snapshots.
- **FR-015**: Successful observations MUST persist as dated portfolio history beginning
  with tracking, retaining asset quantities, available prices and coverage information.
- **FR-016**: The owner MUST view history for 24 hours, 7 days, 30 days and all recorded
  time. Missing periods, partial observations and portfolio membership changes MUST
  be visible. The view MUST NOT label value change as PnL.
- **FR-017**: The owner MUST configure balance and price refresh intervals, pause/resume
  scheduled work and trigger manual refresh. Invalid intervals MUST be rejected.
- **FR-018**: The UI MUST show current work, last attempt/success, next execution and
  failures. Rate limits, timeouts and restarts MUST have bounded recovery behavior.
- **FR-019**: Repeated/overlapping work MUST NOT duplicate snapshots or inflate totals.
  Observations superseded by network reorganizations MUST be corrected or marked invalid.
- **FR-020**: Configuration and committed history MUST survive application restart.
  The owner MUST be able to export data through the UI; ordinary exports MUST exclude
  secrets. Current and full-history exports MUST retain exact numeric strings, source
  and observation times, read/quote status, discovery coverage, exclusion and membership
  context, and snapshot provenance. Unknown monetary values MUST remain explicitly
  unknown, never zero. Full backup/restore procedures, automation and acceptance tests
  are backlog.
- **FR-021**: The UI and documentation MUST be English-only; USD is the valuation currency.
  Wallet setup, holdings, charts and settings MUST remain usable on desktop and mobile
  browser widths, with keyboard-operable controls and textual alternatives to charts.
- **FR-022**: The application MUST be deployable through Docker Compose. Routine
  application settings MUST NOT require editing configuration files or using a terminal.
- **FR-023**: The system MUST never request wallet private keys, seed phrases, signatures,
  spending permissions or transaction execution.
- **FR-024**: External data requests MUST use configured connections with disclosure of
  transmitted data. Credentials MUST be protected in storage and redacted from logs;
  no portfolio telemetry may be sent without explicit owner configuration.

### Key Entities

- **Owner**: The installation's single authenticated operator and password credential.
- **Network connection**: Ethereum identity, endpoint, protected credentials and health.
- **Wallet**: Unique public address, label and active tracking membership.
- **Asset**: Network, native/contract identity, symbol/name, precision and exclusion state.
- **Holding observation**: Wallet, asset, exact quantity, observed block/time and read status.
- **USD quote**: Asset, price, provider, observation time and availability/freshness.
- **Portfolio snapshot**: Dated holdings/valuation with coverage and membership context.
- **Scan run**: Trigger, scope, progress, observation identity, attempts and outcome.
- **Settings**: Shared integration choices, schedules and owner preferences.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With valid connection details available, the owner can complete password
  setup, configure access and add two wallets through the browser within five minutes,
  excluding external scan time.
- **SC-002**: Controlled fixtures covering 0, 6 and 18 token decimals, duplicate symbols,
  tiny/large quantities and repeated addresses produce exact underlying quantities;
  displayed USD totals agree with decimal reference calculations to the displayed cent.
- **SC-003**: Every displayed failed/unpriced holding has a visible status; outage tests
  produce zero cases where an unavailable value is silently displayed as a fresh zero.
  In a two-wallet fixture with one failed refresh, the estimate MUST include that wallet's
  last successful quantities and identify their stale contribution and timestamps;
  a never-successful wallet MUST instead cause an explicit incomplete-estimate state.
- **SC-004**: A reference dataset of 50 wallets, 100 held asset identities and one year
  of hourly snapshots opens the stored dashboard and selected history view within
  three seconds in at least 95 of 100 warm local-network trials on the reference machine
  recorded in the plan. External scan/discovery time is measured separately.
- **SC-005**: Schedule changes survive restart; paused work does not run; repeated manual
  requests during a run produce one active scan and no duplicate snapshot.
- **SC-006**: Application restart tests preserve all committed fixture wallets,
  settings and snapshots with persistent storage intact; exported records reconcile
  with the same observed history.
- **SC-007**: Unauthenticated attempts cannot read portfolio/settings data or reopen
  owner setup. Normal logs, saved-secret views and ordinary exports expose no credentials.
- **SC-008**: All four user journeys can be completed using the English interface at
  390-pixel and 1440-pixel viewport widths, with controls reachable by keyboard and
  chart information available as text.

### Required Verification

Automated checks MUST cover accounting, identity/deduplication, authentication, partial
source failures, snapshot integrity and restart recovery. Integration checks MUST cover
connection validation, actual balance-read behavior against controlled sources and
persistence across application restarts. Browser checks MUST cover setup, settings and portfolio/history inspection.
Discovery checks MUST cover catalog-held/unheld tokens, manual additions outside the
catalog, duplicate contracts across both sources and visible limits to scan coverage.

## Assumptions

- The reference dataset in SC-004 is a proposed engineering benchmark, not a user-stated
  maximum or a promise of scan speed on arbitrary provider plans.
- The owner controls deployment and protects initial setup access. The technical plan
  will define bootstrap protection, transport security and password recovery.
- A selected USD source provides quotes for some assets; universal pricing is not promised.
  Source choice and running costs remain technical planning decisions.
- Staleness thresholds and minimum supported polling intervals will be documented in
  the technical plan and shown in settings; no fixed polling frequency is imposed here.
- History retains recorded observations until the owner explicitly deletes data;
  downsampling or automatic retention limits require a documented feature decision.
- Chart ranges and valuation exclusions are proposed defaults
  derived from usable portfolio operation and remain reviewable.
- News, AI, notifications, activity/security detection, additional networks, automatic
  network discovery, historical transfer-log discovery, SaaS, DeFi position accounting,
  NFTs, historical PnL and full backup/restore are excluded.
