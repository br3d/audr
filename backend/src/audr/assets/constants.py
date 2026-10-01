"""Canonical identity of the native ETH pseudo-asset (AUD-360).

Native ETH has no ERC-20 contract, so it is carried through the balance,
valuation, and quote paths under a sentinel ``token_address``. Two
incompatible sentinels used to coexist — the jobs and price providers wrote
``0xeeee…eeee`` while the ``/portfolio`` and ``/assets`` readers compared
against ``0x0000…0000`` — so ``is_native`` was always ``False`` and native
holdings fell through to an ``UNKNOWN`` placeholder asset.

``0xeeee…eeee`` is the surviving sentinel: it is what already sits in the
database and in the provider price maps. Every writer and reader must import
it from here rather than redeclaring a literal.
"""

from __future__ import annotations

#: The one address under which native ETH is stored and queried.
NATIVE_ETH_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

#: Superseded sentinel, kept only so data migrations and normalisation can
#: recognise rows written before AUD-360.
LEGACY_NATIVE_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"

NATIVE_ETH_SYMBOL = "ETH"
NATIVE_ETH_NAME = "Ethereum"
NATIVE_ETH_DECIMALS = 18

#: ``asset.source`` is constrained to ``catalog``/``manual``. Native ETH is
#: system-known rather than owner-entered, so it is a ``catalog`` asset; the
#: ``/assets`` reader reports its ``kind`` as ``native`` regardless.
NATIVE_ETH_SOURCE = "catalog"


def is_native_eth(token_address: str | None) -> bool:
    """Return whether *token_address* denotes native ETH.

    Accepts the legacy sentinel as well so that rows predating AUD-360 are
    still recognised as native instead of silently reading as an ERC-20.
    """
    if not token_address:
        return False
    normalised = token_address.strip().lower()
    return normalised in (NATIVE_ETH_ADDRESS, LEGACY_NATIVE_ETH_ADDRESS)


def normalise_token_address(token_address: str) -> str:
    """Lowercase *token_address*, collapsing the legacy native sentinel.

    ``asset.token_address`` carries a ``lower(token_address)`` check
    constraint, and the legacy native sentinel must never be re-inserted.
    """
    normalised = token_address.strip().lower()
    if normalised == LEGACY_NATIVE_ETH_ADDRESS:
        return NATIVE_ETH_ADDRESS
    return normalised
