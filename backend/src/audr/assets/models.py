"""SQLAlchemy ORM models for assets, catalog, and discovery (T031 / US1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from audr.models import Base


class Asset(Base):
    """Known ERC-20 token with normalised lowercase address."""

    __tablename__ = "asset"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    token_address: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    decimals: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    decimals_override: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    price_unavailable_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AssetMetadataRevision(Base):
    """Append-only history of asset metadata changes."""

    __tablename__ = "asset_metadata_revision"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    decimals: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogVersion(Base):
    """A pinned snapshot of the token catalog."""

    __tablename__ = "catalog_version"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    commit_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    chain_id: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogEntry(Base):
    """Single ERC-20 entry within a catalog version."""

    __tablename__ = "catalog_entry"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_version.id"), nullable=False
    )
    token_address: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    decimals: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class CmcMapVersion(Base):
    """A pinned snapshot of CoinMarketCap's keyless `/cryptocurrency/map` catalog."""

    __tablename__ = "cmc_map_version"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CmcMapEntry(Base):
    """Single CoinMarketCap id within a map version, keyed for address/symbol lookup."""

    __tablename__ = "cmc_map_entry"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cmc_map_version.id"), nullable=False
    )
    cmc_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    # The token's address on Ethereum mainnet (platform.id == 1 in the CMC
    # response), lowercased. NULL when the coin isn't a known Ethereum ERC-20
    # (e.g. it's native to another chain, or CMC simply has no platform entry
    # for it) — such coins are only reachable via symbol match.
    eth_address: Mapped[str | None] = mapped_column(Text, nullable=True)


class MonitoredPair(Base):
    """Wallet × asset pairs explicitly tracked for balance scanning."""

    __tablename__ = "monitored_pair"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallet.id"), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DiscoveryCoverage(Base):
    """Last-discovery timestamps and checkpoints per wallet."""

    __tablename__ = "discovery_coverage"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallet.id"), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    checkpoint: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class BalanceObservation(Base):
    """Append-only balance snapshot (exact integer raw units)."""

    __tablename__ = "balance_observation"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallet.id"), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.id"), nullable=False)
    raw_amount: Mapped[int] = mapped_column(Numeric(precision=78, scale=0), nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
