"""Keyless public Ethereum RPC endpoints used as the zero-config default (AUD-364).

Every external provider on audr must work out of the box without an API key;
a keyed provider (Infura, Alchemy, …) is an optional upgrade, not a
prerequisite. These endpoints need no account, no key and no billing, so a
fresh install — or an install whose keyed provider has run out of quota and
started answering HTTP 402 — still gets live chain data.

Ordered by preference. All three were verified to answer ``eth_chainId`` with
``0x1`` over plain HTTPS without credentials.
"""

from __future__ import annotations

DEFAULT_PUBLIC_RPC_URLS: tuple[str, ...] = (
    "https://ethereum-rpc.publicnode.com",
    "https://cloudflare-eth.com",
    "https://1rpc.io/eth",
)
