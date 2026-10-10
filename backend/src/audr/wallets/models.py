"""SQLAlchemy ORM models for wallet tracking (T031 / US1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from audr.models import Base


class Wallet(Base):
    """Tracked Ethereum address.

    ``label`` is not a mapped column — only ``label_ciphertext`` (an AES-256-GCM
    envelope, see ``audr.wallets.service``) is persisted. The plaintext label is
    attached as a transient, unmapped attribute by the service layer after
    decrypting it, so API code can keep reading ``wallet.label`` unchanged
    (AUD-488). A freshly-loaded ``Wallet`` that has not gone through the service
    layer's attach step has no ``.label`` attribute at all.
    """

    __tablename__ = "wallet"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    address: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
