<!--
Sync Impact Report
Version: 1.0.0 -> 2.0.0 (owner explicitly deferred mandatory backup/restore)
Modified: Principle I, Owner Control and Explicit Data Sharing; release scope clarified.
Added sections: none. Removed sections: none.
Templates reviewed: plan-template.md, spec-template.md, tasks-template.md; no changes needed.
Feature spec, plan, research, data model, contracts, quickstart and roadmap synchronized.
Backup/restore is backlog; ordinary restart persistence and migration tests remain required.
No deferred placeholders. Installed skill guidance remains applicable.
-->
# AUDR Crypto Constitution

## Core Principles

### I. Owner Control and Explicit Data Sharing

The application MUST be independently deployable and keep its configuration, portfolio
records and history on the owner's installation. Routine product configuration MUST be
available through the authenticated web interface. External integrations MUST disclose
what data they receive and use owner-configured credentials where required.
Credentials MUST be protected at rest, redacted from logs and excluded from ordinary exports.
Cloud AI MUST receive only asset identities and portfolio proportions by default;
wallet addresses and absolute amounts require explicit opt-in. Local AI MAY receive a
fuller context under owner-controlled settings. Data export and persistence across ordinary
application restarts MUST be documented and verifiable. Full backup/restore is backlog
and MUST NOT be required for release 1 unless explicitly reprioritized by the owner. Self-hosting does not imply that configured external services
cannot observe requests.

### II. Read-Only Financial Capabilities

The application MUST observe public addresses and provide information and recommendations.
It MUST NOT request or store wallet private keys or seed phrases, sign transactions,
approve spending, or execute trades. AI recommendations MUST remain informational.
Read-only describes blockchain capabilities; owner settings and local records remain writable.

### III. Exact Accounting and Honest Coverage

Assets MUST be identified by chain and contract address, with a separate native-asset identity.
Quantities and monetary calculations MUST preserve decimal precision; binary floating-point
MUST NOT be used as the authoritative financial representation.
Unavailable balances and prices MUST NOT be represented as zero.
Views and snapshots MUST expose observation times, stale data and incomplete coverage.
Portfolio history MUST distinguish observed value from investment returns and MUST NOT
fabricate periods before tracking began. Duplicate addresses, repeated scans and retries
MUST NOT inflate balances or history. Discovery coverage and its limitations MUST be explicit.

### IV. Evidence-Based AI

News summaries and recommendations MUST retain source links, publication/observation times,
affected assets and the evidence supporting their conclusions. Facts, unverified reports
and model inferences MUST be distinguishable. Repeated publication of the same claim MUST
NOT be treated as independent corroboration. Recommendations MUST explain relevance,
uncertainty and material risks. Source content MUST be treated as untrusted data, never as
authority to change application settings or disclose secrets. AI unavailability MUST NOT
disable portfolio accounting or deterministic monitoring rules.

### V. Reliable and Cost-Aware Background Work

Owners MUST be able to configure, pause and manually trigger applicable polling jobs.
The UI MUST expose last success, current state, failures and the next scheduled execution.
Timeouts, bounded retries, rate limits and restart recovery MUST be defined in feature plans.
Repeated processing MUST be idempotent. Polling settings MUST explain the tradeoff between
request volume and freshness without promising discovery before a source is observed.
When notifications are introduced, delivery status, retry handling and duplicate suppression
MUST be explicit; provider acceptance MUST NOT be represented as confirmed human receipt.

### VI. Replaceable Integrations and Proportionate Architecture

Blockchain access, discovery, pricing, news, AI and notification integrations MUST have
separable responsibilities. Ethereum balance tracking MUST be possible with standard
owner-selected RPC access and MUST NOT require a proprietary portfolio/indexer service.
Optional integrations MAY improve coverage or performance when explicitly configured.
Cloud and local AI MUST both be supported in the AI release. Technologies MUST be chosen
for the current single-owner deployment; speculative SaaS infrastructure MUST NOT expand
the current scope. Significant architectural choices MUST record rationale and alternatives.

### VII. Verifiable Incremental Delivery

Each feature MUST have bounded scope, acceptance scenarios and measurable success criteria.
Critical financial calculations, authentication boundaries, failure handling and persistence
MUST have automated verification. AI and notification features MUST additionally verify
provenance, disclosure boundaries and failure/retry behavior when introduced.
Plans and tasks MUST trace work to requirements and document operational limitations.
Changes to persistent data MUST include a tested migration or recovery path.
User interfaces and project documentation MUST be written in English.

## Product Constraints

- Standalone self-hosted application; one owner and one installation-wide set of integration
  credentials, supporting multiple wallet addresses. Multi-user SaaS is out of scope.
- Docker Compose is the target deployment method. Host provisioning is an operator concern;
  routine application configuration belongs in the web UI.
- One owner password is created during initial setup; subsequent access is authenticated.
  Setup MUST be a one-time operation and the deployment guide MUST protect initial ownership.
- Release 1: Ethereum mainnet, native ETH and ERC-20 balances, USD valuation, allocation
  views and history collected from onboarding onward.
- Release 2: EVM-focused personalized news and read-only recommendation cards; internet
  search, X and aggregators as source categories; no advisor chat or Telegram ingestion.
- Release 3: address activity/security monitoring, urgent news alerts, Telegram and Gotify.
  Detection scope and threat model require dedicated specification before implementation.
- Additional EVM networks, automatic network discovery and other notification channels are
  backlog work unless moved into a release explicitly. DeFi position accounting, NFT
  valuation, historical PnL and transaction execution are outside these release commitments.

## Development Workflow

Constitution -> feature specification -> clarification -> technical plan -> tasks ->
consistency analysis -> implementation and validation.
Overall vision and roadmap provide context; a feature specification defines its deliverable.
Every plan MUST evaluate all applicable constitutional principles and record evidence or a
reasoned not-applicable outcome. Every task list MUST include constitution-required
verification, even where a generic template describes tests as optional.
Feature specifications MUST separate confirmed requirements, assumptions and open decisions.
Provider research MUST assess capabilities, disclosure, cost and failure behavior before
creating provider-specific implementation tasks. Source code from reference projects MUST
undergo dependency and license review before reuse.

## Governance

This constitution governs project specifications, plans, tasks and implementation.
User decisions in the project conversation take precedence and MUST be reflected in the
affected documents. Amendments MUST record rationale, affected principles, migration impact
and a synchronization report. Version changes follow semantic versioning: MAJOR for
incompatible principle changes, MINOR for new or materially expanded principles, PATCH for
clarifications without changed obligations. Reviews MUST check constitutional compliance.
An unresolved violation cannot be silently waived; revise the design or amend the
constitution through an explicit documented decision.

**Version**: 2.0.0 | **Ratified**: 2026-09-25 | **Last Amended**: 2026-09-25
