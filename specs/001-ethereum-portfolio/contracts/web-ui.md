# Web UI Contract

English only, USD only. Responsive at 390px and 1440px; keyboard-accessible controls,
visible focus, labeled form errors, semantic tables and textual chart alternatives.
Keep technical diagnostics in settings/details; the dashboard explains value and freshness.

## Screens

| Screen | Required contents and actions |
|--------|-------------------------------|
| Initial setup | Create/confirm password, clear local-first setup guidance; unavailable after owner creation |
| Sign in | Password, generic failure/cooldown, paste support; no registration or advisor chat |
| Dashboard | USD total or labeled priced subtotal, asset allocation, Ethereum allocation, history, freshness and coverage |
| Wallets | Add any valid public mainnet address/label without proof of control, edit label, stop/reactivate tracking, per-address balances and scan progress; use tracked-address wording without an ownership claim |
| Assets | Holdings, contract/native identity, quantities, prices, excluded view, manual contract addition, decimal conflict resolution |
| Settings: connections | RPC and Demo quote setup, validation, credential replacement, explicit private RPC host permission, disclosure summaries |
| Settings: schedules | Independent balance/discovery/quote intervals, pause, next execution, estimated request volume and local limits |
| Settings: account/data | Change password, logout, private exports, provider-data removal with preview and confirmation |
| Status | Job progress/errors, last successes, worker health, catalog version and coverage; safe actionable errors |

## Dashboard behavior

Lead with portfolio value, then allocation and history. Keep wallet/asset detail searchable
and paginated. Every unavailable figure is unknown/unpriced, not "$0.00".
For a tiny nonzero holding show an appropriate significant representation rather than zero.
All visible quantities and tooltips derive from decimal strings.

Show Refresh balances and Discover tokens as separate actions. Explain that refreshing
known balances does not discover contracts outside catalog/manual coverage. Full discovery
shows attempted/total counts and the last completed time for each wallet. A partial scan
does not imply that no other assets are held.

Allocation includes only non-excluded priced holdings. Explain incomplete or stale basis.
No chart slice for zero/unknown total. Ethereum is the only network in release 1; avoid
controls suggesting other networks already work.
When all known nonzero holdings are excluded and included inputs are known after every
active wallet has a successful observation, show a zero included total, an excluded-only
explanation and no allocation chart. An unknown included input keeps the total unknown.
Balance freshness uses the last successful verified block time; show read completion
time separately. A later verified block refreshes freshness even if quantity is unchanged.

History ranges: 24h/7d/30d/all. The chart uses actual recorded observations. Display gaps
and membership/exclusion-change markers. Do not label value movement "profit" or "return".
Chart summary text/table must provide the same information without relying on color.
Changing exclusions today does not alter old portfolio composition.

## Setup and connection flow

After creating a password, offer Ethereum RPC setup, optional USD quote setup and wallet
addition. Balances work before a quote key is entered. Never request a wallet connection,
signature, private key or seed phrase.
Any valid public address can be tracked without proving control; labels describe tracked
addresses and must not imply that the application verified ownership.

RPC disclosure: endpoint sees addresses queried and requested contracts. Quote disclosure:
source sees asset identifiers and server IP, not wallet addresses or amounts. Catalog
discovery uses bundled data. No external fonts, token-logo fetches or analytics by default.

Mask saved credentials and URLs containing credentials. Test connection through the server;
wrong network and unsupported safe-block capability receive actionable messages.
Catalog metadata is a candidate description, not a token safety endorsement.

## Empty, failure and destructive states

- No wallets: show Add wallet; no fabricated total/history.
- Excluded-only holdings after successful observations: show a zero included total,
  visible excluded assets and no allocation chart; keep a null total for unknown included inputs.
- First scan: progress plus available observations; no promise of fixed completion time.
- RPC down: retain last-known holdings, visible stale/error status and connection action.
- Quotes down/unconfigured: keep quantities, show partial/unpriced value and setup/status.
- Budget exceeded: show paused reason, reset time and edit-limits action; never auto-upgrade.
- Worker down: show background-work degraded while saved views remain available.
- Metadata conflict: display raw amount/contract and resolve-decimals action.
- Stop wallet tracking: explain current-total removal and retained historical records.
- Provider-data purge: preview affected records, require password and explicit confirmation;
  explain that independently retained copies are outside this operation. Never run this on ordinary disable.

## Acceptance mapping

US1: setup/wallets/assets; US2: dashboard/assets; US3: history and scope markers;
US4: connections/schedules/status/account-data.
SC-008 requires all four journeys on both viewport sizes with keyboard and textual charts.
SC-001 excludes network scan time, not the time needed to understand setup.
