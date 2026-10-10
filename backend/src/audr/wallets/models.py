"""SQLAlchemy ORM models for wallet tracking (T031 / US1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from audr.models import Base


class Wallet(Base):
    """Tracked Ethereum address.

    Neither ``address`` nor ``label`` is a mapped column — only their
    ciphertext envelopes (AES-256-GCM, see ``audr.wallets.service``) are
    persisted. The plaintext values are attached as transient, unmapped
    attributes by the service layer after decrypting them, so API code can
    keep reading ``wallet.address`` / ``wallet.label`` unchanged (AUD-488,
    AUD-490). A freshly-loaded ``Wallet`` that has not gone through the
    service layer's attach step has neither attribute at all.

    Uniqueness on the address moved from the (now-gone) plaintext column to
    ``address_bidx``, a deterministic HMAC-SHA256 of the normalised address
    keyed by a subkey derived from the master key — distinct from the key used
    for encryption, so the index key leaking does not help decrypt envelopes
    and vice versa.
    """

    __tablename__ = "wallet"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    address_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    address_bidx: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    label_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
