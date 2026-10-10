"""Shared helpers for tests that build `wallet` rows via raw SQL, bypassing
the service layer (`audr.wallets.service.add_wallet`), e.g. to control the
row's id or to seed fixtures in bulk.

Since AUD-490, `wallet.address` is no longer a plaintext column — a raw
INSERT must supply `address_ciphertext` and `address_bidx` itself. This
mirrors exactly what `add_wallet` does, using the same master key every
integration test already has via the `_master_key_ready` autouse fixture
(tests/integration/conftest.py).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.init_key import get_master_key
from audr.wallets.service import compute_address_bidx, encrypt_address


async def wallet_address_columns(
    session: AsyncSession, wallet_id: uuid.UUID, address: str
) -> dict[str, bytes]:
    """Return ``{"address_ciphertext": ..., "address_bidx": ...}`` for a raw
    ``INSERT INTO wallet`` statement, matching what `add_wallet` would store
    for *address* on *wallet_id*. *address* is lowercased before encrypting —
    callers that need a specific case preserved on read should normalise
    before calling this and reuse the same normalised value for assertions.
    """
    key = await get_master_key(session)
    normalised = address.lower()
    return {
        "address_ciphertext": encrypt_address(normalised, wallet_id, key),
        "address_bidx": compute_address_bidx(normalised, key),
    }
