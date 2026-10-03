"""SQLAlchemy ORM models for quotes and valuation snapshots (T051 / US2 / AUD-64)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from audr.models import Base


class QuoteSet(Base):
    """A batch fetch of prices from one provider at a point in time."""

    __tablename__ = "quote_set"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class QuoteObservation(Base):
    """Individual price point per asset within a quote set."""

    __tablename__ = "quote_observation"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quote_set_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quote_set.id"), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.id"), nullable=False)
    # Exact USD price: 36 total digits, 18 decimal places.
    price_usd: Mapped[object] = mapped_column(Numeric(precision=36, scale=18), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ValuationSnapshot(Base):
    """Point-in-time portfolio valuation.  Immutable once published."""

    __tablename__ = "valuation_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshotted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    quality: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # sha256 of the exact inputs (observation ids + quote_set ids) this
    # snapshot was built from — unique so publish_valuation_snapshot can
    # dedupe a retry of the same inputs instead of publishing a duplicate.
    input_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class ValuationLine(Base):
    """One holding line (wallet × asset) within a valuation snapshot."""

    __tablename__ = "valuation_line"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("valuation_snapshot.id"), nullable=False
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallet.id"), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.id"), nullable=False)
    raw_amount: Mapped[object] = mapped_column(Numeric(precision=78, scale=0), nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL when price was not available at snapshot time (unknown ≠ zero).
    price_usd: Mapped[object | None] = mapped_column(Numeric(precision=36, scale=18), nullable=True)
    value_usd: Mapped[object | None] = mapped_column(Numeric(precision=36, scale=18), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
