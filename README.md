# AUDR Crypto

A standalone self-hosted Ethereum portfolio, evolving into EVM-focused news,
read-only AI recommendations and address security monitoring.

Status: requirements and specification; no application implementation yet.
AUDR Crypto is a working name.

- [Constitution](.specify/memory/constitution.md)
- [Product vision](docs/product-vision.md)
- [Release roadmap](docs/roadmap.md)
- [Decisions and research](docs/discovery.md)
- [Release 1 specification](specs/001-ethereum-portfolio/spec.md)
- [Release 1 technical plan](specs/001-ethereum-portfolio/plan.md)
- [Specification quality checklist](specs/001-ethereum-portfolio/checklists/requirements.md)

The target deployment is Docker Compose with English web-based configuration.
Release 1 tracks Ethereum mainnet ETH and ERC-20 balances in USD for one owner.
The application never holds wallet private keys or executes transactions.

Token discovery uses a known-token catalog and manual contract additions through RPC.
The technical plan selects Python/FastAPI, PostgreSQL, React and an optional CoinGecko
Demo quote adapter. See its research for provider budgets and limitations.
Next: generate implementation tasks with speckit-tasks, then check consistency with
speckit-analyze. No application has been implemented yet.
