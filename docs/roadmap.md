# Release Roadmap

## Release 1 — Ethereum Portfolio

One-time owner setup and authentication; web configuration; Ethereum mainnet RPC;
multiple addresses; ETH and ERC-20 holdings; USD quotes; total and asset/network
allocation; history from connection onward; adjustable scanning and manual refresh.
Persistence across restarts, export and honest failure/coverage states support operation.
Ethereum is the only selectable network initially; its network allocation is 100%.

Current artifact: [specification](../specs/001-ethereum-portfolio/spec.md).
Discovery uses a maintained token catalog and manual contract additions through RPC.
Historical transfer-log discovery is outside this release.
The [technical plan](../specs/001-ethereum-portfolio/plan.md) is complete; next are
implementation tasks and consistency analysis.
No AI, news, notifications or address-security detector is required in this release.
Application authentication and secret protection are still release-1 requirements.

## Release 2 — AI News and Recommendations

Personalized EVM news sourced through internet search, X and aggregators.
Owner-selected assets; cloud and local AI; configurable polling; deduplicated stories;
source links and times; read-only recommendation cards and configurable advisor instructions.
No advisor chat or Telegram news ingestion. External notification delivery starts in release 3.

Before specification: choose access methods and budgets, define source reliability,
unverified claims, corrections, watchlist semantics, recommendation expiration and
minimum capabilities for locally hosted AI and search.

## Release 3 — Notifications and Address Security

Telegram and Gotify delivery for configured events, including urgent news.
Address activity monitoring and a separately specified security module.
Define rules, severity, thresholds, evidence, duplicate suppression and delivery recovery.
Polling and alert preferences are owner-controlled.

A dedicated discussion must define the threat model and detection coverage.
Candidate areas include outgoing transfers, approvals, large outflows and suspicious
contract interactions. These are discussion topics, not promised detection capabilities.
Balance changes alone do not prove compromise; lack of alerts does not prove safety.

## Backlog

Full backup and restore: procedures, tooling, automation and validation. Explicitly
removed from release 1 by the owner during clarification; export is not a full backup.

Additional EVM networks and opt-in network discovery; email and other channels;
multi-owner SaaS. DeFi accounting, NFTs and historical PnL require separate prioritization.
Do not implement backlog infrastructure merely to anticipate a possible future release.

## Spec Kit progression

The constitution is project-wide. Each release can be split into bounded feature specs.
For release 1: specify -> clarify -> plan -> tasks -> analyze -> implement -> converge.
Plans must settle provider choices, limits, cost assumptions and deployment recovery.
Tasks must follow the plan and include constitution-required verification.
Implementation starts after the feature artifacts are ready.
