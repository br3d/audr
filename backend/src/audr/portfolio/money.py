"""Exact decimal arithmetic for portfolio valuations (T055 / US2 / AUD-68).

Rules:
- uint256 raw integer units → human-readable Decimal via raw_to_quantity().
- Multiply quantity × price_usd for exact USD value via quantity_to_usd().
- All calculations use Python Decimal — never float.
- format_decimal() produces a fixed-point string safe for API responses.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


class DecimalsRangeError(ValueError):
    """Raised when decimals is outside the valid ERC-20 range (0–77)."""


def raw_to_quantity(raw: int, decimals: int) -> Decimal:
    """Convert a raw uint256 integer amount to a human-readable Decimal.

    Example: raw=1_000_000, decimals=6  →  Decimal('1')
    """
    if not isinstance(raw, int):
        raise TypeError(f"raw must be int, got {type(raw).__name__}")
    if raw < 0:
        raise ValueError(f"raw must be non-negative, got {raw}")
    if not (0 <= decimals <= 77):
        raise DecimalsRangeError(f"decimals must be 0–77, got {decimals}")
    return Decimal(raw) / (Decimal(10) ** decimals)


def quantity_to_usd(quantity: Decimal, price_usd: Decimal) -> Decimal:
    """Multiply quantity by price to get exact USD value.

    Both arguments must be Decimal.  Returns a Decimal — caller decides rounding.
    """
    if not isinstance(quantity, Decimal):
        raise TypeError(f"quantity must be Decimal, got {type(quantity).__name__}")
    if not isinstance(price_usd, Decimal):
        raise TypeError(f"price_usd must be Decimal, got {type(price_usd).__name__}")
    return quantity * price_usd


def format_decimal(value: Decimal, *, places: int = 18) -> str:
    """Format a Decimal to a fixed-point string with *places* decimal places.

    Uses ROUND_DOWN (truncate) to avoid overstating values in display.
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"value must be Decimal, got {type(value).__name__}")
    if places < 0:
        raise ValueError(f"places must be non-negative, got {places}")
    quantizer = Decimal(10) ** -places
    return str(value.quantize(quantizer, rounding=ROUND_DOWN))
