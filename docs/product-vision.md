# Product Vision

## Purpose

Give one owner a consolidated view of public EVM wallet holdings and, in later releases,
explain relevant news and potentially significant activity without surrendering custody.
The application is independently developed; rotki is a product reference.

## Owner experience

Deploy the application, create an owner password, configure data connections in the web
interface and add wallet addresses. View native and token quantities, their USD value,
allocation and history collected since tracking began. See what was scanned and when.

Later, choose assets for a personalized news feed and configure a local or cloud AI.
Read sourced summaries and recommendation cards that explain their relevance to the
portfolio. Configure schedules according to acceptable cost and freshness.
Finally, route urgent news and activity/security findings to Telegram and Gotify.

## Boundaries

- One installation, one owner, many wallets, shared integration credentials.
- Ethereum mainnet first; EVM only for the initial product direction.
- ETH and ERC-20 holdings, not DeFi position decomposition or NFT valuation.
- Observed portfolio value history, not historical accounting or investment PnL.
- English interface and documentation; USD valuation.
- Web configuration for routine operation; Docker Compose for deployment.
- Advisory output only; no chat, private keys, signing or transaction execution.
- SaaS, additional notification channels and automatic network discovery are backlog.

## Trust and quality

Unknown values remain unknown. Incomplete discovery is visible. An asset is identified
by its network and contract, not its symbol. News claims remain linked to sources;
AI interpretation is labeled. Third-party requests are controlled through settings.
Cloud AI defaults to asset identities and proportions; addresses and amounts require opt-in.

## Measures of value

Release 1 succeeds when the owner can configure and inspect a multi-wallet portfolio
through the browser, with accurate supported balances, explainable valuation and persistent
history. Later releases add relevant sourced information and actionable awareness with
explicit uncertainty, controllable polling and observable notification delivery.

The [release-1 specification](../specs/001-ethereum-portfolio/spec.md) defines measurable
acceptance criteria. The [roadmap](roadmap.md) defines subsequent release boundaries.
