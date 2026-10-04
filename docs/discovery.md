# Discovery and Decision Record

Date: 2026-09-25. Working project name: AUDR Crypto (from the workspace name).

## Confirmed decisions

The owner wants a standalone self-hosted application inspired by rotki. The current scope
is one owner, multiple wallet addresses and one installation-wide set of integration
credentials. SaaS is a possible future direction, outside the current scope.

Release 1 covers Ethereum mainnet, native ETH and ERC-20 balances, USD valuation,
asset/network allocation and history starting when addresses are connected.
Networks are selected explicitly; more EVM networks and automatic network discovery
are future enhancements. DeFi accounting, NFT valuation and historical PnL are not included.

Release 2 adds an EVM-focused personalized news feed and recommendation cards.
Sources include internet search, X and aggregators; Telegram ingestion is excluded.
Owners select relevant assets. Both cloud and local AI are required.
Cloud AI receives asset identities and proportions by default, without wallet addresses
or absolute amounts. Broader disclosure requires opt-in. Recommendations are read-only;
there is no advisor chat, transaction signing or execution.

Release 3 adds notifications and address activity/security monitoring.
Telegram and Gotify are the initial notification channels; email and other channels
are backlog. Security coverage needs a dedicated threat-model discussion and specification.
Polling intervals are owner-configurable to balance cost and freshness.

Docker Compose is the preferred deployment. All routine application settings are managed
through the web UI. The owner creates a password at initial setup.
The UI and all project documentation are English-only. Conversation language is Russian.
The implementation stack is open for technical selection.

## Research: Ethereum RPC and token discovery

The earlier suggestion that a dedicated external discovery API is required was too strong.
Standard RPC is sufficient to query ETH and the balances of known ERC-20 contracts.

Reviewed [eth-balance-checker main.py](https://github.com/br3d/eth-balance-checker/blob/main/main.py):
it creates a PyEtherBalance instance with the configured RPC endpoint; main_check iterates
over configured addresses and coins_list; check_token_balance calls get_token_balance.
This is scanning a supplied set of tokens, rather than discovering every unknown contract.

[PyEtherBalance documentation](https://pypi.org/project/pyetherbalance/) says that its token
catalog comes from ethereum-lists/tokens and supports custom contract entries.
A catalog can therefore provide candidate contracts without a portfolio/indexer API.

[Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/) provides balance,
contract-call and log-reading operations, but no universal method listing every token held
by an address. Alternatives include a token catalog, Transfer-log discovery or an optional
indexer. Log discovery requires historical log access and range handling; nonstandard
tokens and incomplete source coverage must not be hidden.

Confirmed decision (option A): scan a maintained known-token catalog through RPC and
support manual contract additions. Historical transfer-log discovery is outside release 1.
No proprietary indexer is required. Coverage must be visible and must not imply that all
possible tokens have been discovered. USD quote selection is separate from balance reading
and remains planning work.
No live RPC scan, performance benchmark or dependency audit was performed in this research.

## Research: reference applications and notifications

[rotki](https://github.com/rotki/rotki) is a self-hosted portfolio/accounting reference,
not the selected codebase. Its repository states AGPLv3 licensing.
[Gotify](https://gotify.net/docs/pushmsg) supports HTTP message submission and priorities.

## Open decisions by phase

- Release-1 planning decisions are recorded in the internal research note:
  CoinGecko Demo quotes,
  bundled Ethereum Lists catalog, configurable RPC limits, reference benchmark and
  persistent key storage and password recovery. Full backup/restore was subsequently
  deferred to backlog by the owner; restart persistence and web export remain in release 1. Exact dependency/catalog pins and measured performance
  remain implementation verification work.
- Before release 2: provider choices, budgets, X access, news trust policy,
  recommendation freshness, asset selection/watchlist details and local AI capabilities.
- Before release 3: threat model, supported detections, false-positive handling,
  alert thresholds, quiet hours, routing, delivery retries and urgent unverified reports.
- Before distribution: project name and licensing choice.

## Workspace observations

Spec Kit 0.13.0 templates and skills are present. No application code or local .git
directory was found. Git tried to use the parent /Users/br3d/git and failed its ownership
check; Git configuration was not changed. Branch operations need a correct project
repository boundary. Installed planning/tasks skills still contain a literal SCRIPT
placeholder and several skills reference an absent .specify/scripts/python directory.
Planning located and ran the official Bash setup helper in the installed CLI core pack.
It resolves this project through feature.json without using the parent Git repository.
The corresponding bundled Bash prerequisites helper can support downstream workflows.
Generated Python references remain stale; a full integration reinstall was not needed.

## Documentation route

See [product vision](product-vision.md) and [roadmap](roadmap.md); the
release-1 specification itself is internal planning material and is not part
of this repository.
The discovery clarification and technical plan are complete. Continue the
[Spec Kit workflow](https://github.com/github/spec-kit) with task generation,
then analyze consistency before implementation.
