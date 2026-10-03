"""Unit tests for portfolio/money.py exact decimal arithmetic (T055 / AUD-68)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from audr.portfolio.money import (
    DecimalsRangeError,
    format_decimal,
    quantity_to_usd,
    raw_to_quantity,
)


@pytest.mark.unit
class TestRawToQuantity:
    def test_basic_erc20(self) -> None:
        # 1_000_000 with 6 decimals = 1 USDC
        result = raw_to_quantity(1_000_000, 6)
        assert result == Decimal("1")

    def test_one_ether(self) -> None:
        # 1 ETH = 10^18 wei
        result = raw_to_quantity(10**18, 18)
        assert result == Decimal("1")

    def test_zero_amount(self) -> None:
        assert raw_to_quantity(0, 18) == Decimal("0")

    def test_zero_decimals(self) -> None:
        # ERC-20 with 0 decimals (indivisible)
        assert raw_to_quantity(42, 0) == Decimal("42")

    def test_large_uint256(self) -> None:
        # max uint256 = 2^256 - 1, should not overflow
        max_uint256 = 2**256 - 1
        result = raw_to_quantity(max_uint256, 18)
        assert result > 0
        assert isinstance(result, Decimal)

    def test_fractional_precision(self) -> None:
        # 1 wei
        result = raw_to_quantity(1, 18)
        assert result == Decimal("1E-18")

    def test_negative_raw_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            raw_to_quantity(-1, 18)

    def test_non_int_raw_raises(self) -> None:
        with pytest.raises(TypeError, match="int"):
            raw_to_quantity(1.0, 18)  # type: ignore[arg-type]

    def test_decimals_out_of_range_raises(self) -> None:
        with pytest.raises(DecimalsRangeError):
            raw_to_quantity(1, 78)

    def test_decimals_negative_raises(self) -> None:
        with pytest.raises(DecimalsRangeError):
            raw_to_quantity(1, -1)

    def test_decimals_max_valid(self) -> None:
        # 77 is the maximum valid value; 10^77 / 10^77 = 1
        result = raw_to_quantity(10**77, 77)
        assert result == Decimal("1")


@pytest.mark.unit
class TestQuantityToUsd:
    def test_basic_multiplication(self) -> None:
        quantity = Decimal("1")
        price = Decimal("2000.00")
        result = quantity_to_usd(quantity, price)
        assert result == Decimal("2000.00")

    def test_fractional(self) -> None:
        quantity = Decimal("0.5")
        price = Decimal("2000")
        result = quantity_to_usd(quantity, price)
        assert result == Decimal("1000")

    def test_large_values(self) -> None:
        quantity = Decimal("1000000")
        price = Decimal("1.000001")
        result = quantity_to_usd(quantity, price)
        assert result == Decimal("1000001")

    def test_exact_no_float(self) -> None:
        # Ensure no float imprecision slips in
        quantity = Decimal("0.1")
        price = Decimal("0.3")
        result = quantity_to_usd(quantity, price)
        assert result == Decimal("0.03")

    def test_non_decimal_quantity_raises(self) -> None:
        with pytest.raises(TypeError, match="Decimal"):
            quantity_to_usd(1.0, Decimal("2000"))  # type: ignore[arg-type]

    def test_non_decimal_price_raises(self) -> None:
        with pytest.raises(TypeError, match="Decimal"):
            quantity_to_usd(Decimal("1"), 2000.0)  # type: ignore[arg-type]


@pytest.mark.unit
class TestFormatDecimal:
    def test_default_18_places(self) -> None:
        result = format_decimal(Decimal("1"))
        assert result == "1.000000000000000000"

    def test_custom_places(self) -> None:
        result = format_decimal(Decimal("1.23456789"), places=4)
        assert result == "1.2345"

    def test_truncation_not_rounding(self) -> None:
        # 1.999...9 truncated to 2 places = "1.99", not "2.00"
        result = format_decimal(Decimal("1.999"), places=2)
        assert result == "1.99"

    def test_zero(self) -> None:
        result = format_decimal(Decimal("0"), places=6)
        assert result == "0.000000"

    def test_zero_places(self) -> None:
        result = format_decimal(Decimal("1.9"), places=0)
        assert result == "1"

    def test_non_decimal_raises(self) -> None:
        with pytest.raises(TypeError, match="Decimal"):
            format_decimal(1.0)  # type: ignore[arg-type]

    def test_negative_places_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            format_decimal(Decimal("1"), places=-1)

    def test_large_value(self) -> None:
        big = Decimal("1234567890.123456789")
        result = format_decimal(big, places=6)
        assert result == "1234567890.123456"
