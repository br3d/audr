"""FastAPI routes for on-chain events (AUD-307) and allowance security signals
(AUD-300).

GET /api/v1/events
  Returns a paginated list of indexed ERC-20 Transfer events, enriched
  (AUD-522) with asset metadata, a decimal `amount`, the latest known
  `price_usd` / `value_usd`, and the owning wallet's `wallet_label` — all
  nullable, since any of them can be unknown (unlisted token, no price yet).
  Filters: wallet_id, event_type, token_address.
  Ordered by block_number DESC, log_index DESC.

GET /api/v1/allowances
  Returns the current (latest observed) ERC-20 allowance per
  (wallet, token, spender), derived from indexed Approval events.
  Flags `is_unlimited` when the approved amount looks like an
  effectively-infinite approval — a standard wallet-security signal.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import ReadPrincipal, _require_read_auth
from audr.db import get_db
from audr.operations.init_key import get_master_key
from audr.portfolio.money import (
    DecimalsRangeError,
    format_decimal,
    quantity_to_usd,
    raw_to_quantity,
)
from audr.wallets.models import Wallet
from audr.wallets.service import decrypt_label

router = APIRouter(prefix="/api/v1")

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

# Approvals at or above this threshold are flagged as "unlimited" — the
# conventional heuristic for effectively-infinite ERC-20 allowances (wallets
# and revocation tools commonly approve type(uint256).max; this threshold is
# far above any realistic token balance regardless of decimals).
_UNLIMITED_ALLOWANCE_THRESHOLD = 2**128


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class OnchainEventResponse(BaseModel):
    id: str
    wallet_id: str
    tx_hash: str
    block_number: int
    log_index: int
    # 'transfer_in' | 'transfer_out'
    event_type: str
    token_address: str
    # The non-owner side of the event; NULL for a self-transfer (owner is
    # both sides) so the owner's own address is never stored (AUD-389 3c).
    counterparty_address: str | None
    # Raw uint256 as decimal string — never float
    raw_amount: str
    indexed_at: str
    # Everything below is additive enrichment (AUD-522) so the UI can render
    # a human-readable line instead of a hash + raw integer. NULL wherever
    # the underlying fact is unknown (e.g. token not in our `asset` catalog,
    # or no price has ever been fetched) — never coerced to zero.
    asset_id: str | None = None
    symbol: str | None = None
    token_name: str | None = None
    decimals: int | None = None
    # raw_amount / 10^decimals as an exact decimal string.
    amount: str | None = None
    # amount * price_usd. Both this and price_usd reflect the latest known
    # price at *request time*, not the price at the event's block — this
    # endpoint does no historical price lookup, so do not present it as such.
    value_usd: str | None = None
    price_usd: str | None = None
    wallet_label: str | None = None


class EventsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    events: list[OnchainEventResponse]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/events", response_model=EventsResponse)
async def get_events(
    _auth: Annotated[ReadPrincipal, Depends(_require_read_auth)],
    db: AsyncSession = Depends(get_db),
    wallet_id: str | None = Query(default=None),
    event_type: Literal["transfer_in", "transfer_out"] | None = Query(default=None),
    token_address: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> EventsResponse:
    """Return a paginated list of indexed on-chain Transfer events.

    Results are ordered newest-first (block_number DESC, log_index DESC).
    `raw_amount` stays the raw uint256 decimal-string source of truth.
    `amount` / `value_usd` / `price_usd` are derived, human-readable
    enrichment: `value_usd` and `price_usd` are the *latest known* price at
    request time (the most recent `quote_set` with `status = 'complete'`),
    not a historical price as of the event's block — this endpoint does not
    do a price-at-block lookup, so callers must not present it as such.
    """
    wallet_uuid: uuid.UUID | None = None
    if wallet_id is not None:
        try:
            wallet_uuid = uuid.UUID(wallet_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="wallet_id is not a valid UUID",
            ) from exc

    conditions = []
    params: dict = {"limit": limit, "offset": offset}

    if wallet_uuid is not None:
        conditions.append("oe.wallet_id = :wallet_id")
        params["wallet_id"] = str(wallet_uuid)
    if event_type is not None:
        conditions.append("oe.event_type = :event_type")
        params["event_type"] = event_type
    if token_address is not None:
        conditions.append("oe.token_address = :token_address")
        params["token_address"] = token_address.lower()

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # `where` is a fixed vocabulary of ":param" fragments; values are bound, never interpolated.
    count_row = await db.execute(
        sa.text(f"SELECT COUNT(*) FROM onchain_event oe {where}"),  # noqa: S608 — where is a fixed vocabulary of ":param" fragments; values are bound, never interpolated
        params,
    )
    total = count_row.scalar_one()

    # Single query for the whole page: asset metadata and the latest known
    # price are joined in, not fetched per row (no N+1). `latest_qs` picks
    # the single most recent complete-with-observations quote_set — the
    # same "latest known price" each row's price/value is read from, so
    # every row on a page is priced against the same snapshot in time.
    rows = await db.execute(
        sa.text(
            f"""
            SELECT oe.id, oe.wallet_id, oe.tx_hash, oe.block_number, oe.log_index,
                   oe.event_type, oe.token_address, oe.counterparty_address,
                   oe.raw_amount, oe.indexed_at,
                   a.id AS asset_id, a.symbol, a.name AS token_name,
                   COALESCE(a.decimals_override, a.decimals) AS decimals,
                   qo.price_usd::text AS price_usd
            FROM onchain_event oe
            LEFT JOIN asset a ON a.token_address = lower(oe.token_address)
            LEFT JOIN (
                SELECT qs.id
                FROM quote_set qs
                WHERE qs.status = 'complete'
                  AND EXISTS (
                      SELECT 1 FROM quote_observation qo2 WHERE qo2.quote_set_id = qs.id
                  )
                ORDER BY qs.fetched_at DESC
                LIMIT 1
            ) latest_qs ON true
            LEFT JOIN quote_observation qo
                ON qo.quote_set_id = latest_qs.id AND qo.asset_id = a.id
            {where}
            ORDER BY oe.block_number DESC, oe.log_index DESC
            LIMIT :limit OFFSET :offset
            """  # noqa: S608 — where is a fixed vocabulary of ":param" fragments; values are bound, never interpolated
        ),
        params,
    )
    event_rows = rows.fetchall()

    # Wallet labels are encrypted (AES-256-GCM) and can only be read via the
    # master key + per-wallet AAD decryption, not a plain SQL join — so this
    # fetches the (few, distinct) wallets for this page in one extra query
    # and decrypts in Python, rather than per-row.
    wallet_ids = {row[1] for row in event_rows}
    wallet_labels: dict[str, str | None] = {}
    if wallet_ids:
        wallet_rows = (
            await db.execute(
                sa.select(Wallet.id, Wallet.label_ciphertext).where(Wallet.id.in_(wallet_ids))
            )
        ).all()
        key = await get_master_key(db)
        wallet_labels = {
            str(wid): decrypt_label(ciphertext, wid, key) or None for wid, ciphertext in wallet_rows
        }

    events = []
    for row in event_rows:
        decimals = int(row[13]) if row[13] is not None else None
        price_usd_str: str | None = row[14]

        amount_str: str | None = None
        quantity: Decimal | None = None
        if decimals is not None:
            try:
                quantity = raw_to_quantity(int(row[8]), decimals)
            except (DecimalsRangeError, ValueError):
                quantity = None
            else:
                amount_str = format_decimal(quantity)

        value_usd_str: str | None = None
        if quantity is not None and price_usd_str is not None:
            value_usd_str = format_decimal(quantity_to_usd(quantity, Decimal(price_usd_str)))

        events.append(
            OnchainEventResponse(
                id=str(row[0]),
                wallet_id=str(row[1]),
                tx_hash=str(row[2]),
                block_number=int(row[3]),
                log_index=int(row[4]),
                event_type=str(row[5]),
                token_address=str(row[6]),
                counterparty_address=str(row[7]) if row[7] is not None else None,
                raw_amount=str(row[8]),
                indexed_at=row[9].isoformat(),
                asset_id=str(row[10]) if row[10] is not None else None,
                symbol=str(row[11]) if row[11] is not None else None,
                token_name=str(row[12]) if row[12] is not None else None,
                decimals=decimals,
                amount=amount_str,
                value_usd=value_usd_str,
                price_usd=price_usd_str,
                wallet_label=wallet_labels.get(str(row[1])),
            )
        )

    return EventsResponse(
        total=total,
        limit=limit,
        offset=offset,
        events=events,
    )


# ---------------------------------------------------------------------------
# Allowances (security signals)
# ---------------------------------------------------------------------------


class AllowanceResponse(BaseModel):
    wallet_id: str
    token_address: str
    # NULL only for the degenerate case of a wallet approving itself as
    # spender — there is no third-party address to show (AUD-389 3c).
    spender_address: str | None
    # Raw uint256 as decimal string — never float
    raw_amount: str
    is_unlimited: bool
    observed_at_block: int
    tx_hash: str
    indexed_at: str


class AllowancesResponse(BaseModel):
    total: int
    limit: int
    offset: int
    allowances: list[AllowanceResponse]


@router.get("/allowances", response_model=AllowancesResponse)
async def get_allowances(
    _auth: Annotated[ReadPrincipal, Depends(_require_read_auth)],
    db: AsyncSession = Depends(get_db),
    wallet_id: str | None = Query(default=None),
    unlimited_only: bool = Query(default=False),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> AllowancesResponse:
    """Return the current ERC-20 allowance per (wallet, token, spender).

    Derived from the latest indexed Approval event for each triple — this is
    a read model over the append-only `onchain_event` log, not a separate
    mutable table. `is_unlimited` flags allowances at or above
    ``_UNLIMITED_ALLOWANCE_THRESHOLD``, the standard "infinite approval"
    security signal.
    """
    wallet_uuid: uuid.UUID | None = None
    if wallet_id is not None:
        try:
            wallet_uuid = uuid.UUID(wallet_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="wallet_id is not a valid UUID",
            ) from exc

    conditions = ["event_type = 'approval'"]
    params: dict = {
        "limit": limit,
        "offset": offset,
        "threshold": str(_UNLIMITED_ALLOWANCE_THRESHOLD),
    }
    if wallet_uuid is not None:
        conditions.append("wallet_id = :wallet_id")
        params["wallet_id"] = str(wallet_uuid)
    where = "WHERE " + " AND ".join(conditions)

    # Latest Approval per (wallet, token, spender); counterparty_address holds
    # the spender for approval-typed rows (see jobs/event_indexer.py) — NULL
    # only for the degenerate self-approval case (owner == spender), which a
    # spender-keyed view has no address to show for anyway. `where` is a
    # fixed vocabulary of ":param" fragments; values are bound, never interpolated.
    latest_cte = f"""
        SELECT DISTINCT ON (wallet_id, token_address, counterparty_address)
            wallet_id, token_address, counterparty_address AS spender_address,
            raw_amount, block_number, tx_hash, indexed_at
        FROM onchain_event
        {where}
        ORDER BY wallet_id, token_address, counterparty_address, block_number DESC, log_index DESC
    """  # noqa: S608 — where is a fixed vocabulary of ":param" fragments; values are bound, never interpolated

    having = "WHERE raw_amount >= :threshold" if unlimited_only else ""

    count_row = await db.execute(
        sa.text(f"SELECT COUNT(*) FROM ({latest_cte}) latest {having}"),  # noqa: S608 — latest_cte/having are fixed vocabularies of ":param" fragments; values are bound, never interpolated
        params,
    )
    total = count_row.scalar_one()

    rows = await db.execute(
        sa.text(
            f"""
            SELECT wallet_id, token_address, spender_address, raw_amount,
                   block_number, tx_hash, indexed_at
            FROM ({latest_cte}) latest
            {having}
            ORDER BY block_number DESC
            LIMIT :limit OFFSET :offset
            """  # noqa: S608 — latest_cte/having are fixed vocabularies of ":param" fragments; values are bound, never interpolated
        ),
        params,
    )

    allowances = [
        AllowanceResponse(
            wallet_id=str(row[0]),
            token_address=str(row[1]),
            spender_address=str(row[2]) if row[2] is not None else None,
            raw_amount=str(row[3]),
            is_unlimited=int(row[3]) >= _UNLIMITED_ALLOWANCE_THRESHOLD,
            observed_at_block=int(row[4]),
            tx_hash=str(row[5]),
            indexed_at=row[6].isoformat(),
        )
        for row in rows.fetchall()
    ]

    return AllowancesResponse(
        total=total,
        limit=limit,
        offset=offset,
        allowances=allowances,
    )
