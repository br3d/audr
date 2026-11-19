"""AUD-963: per-asset move arithmetic. Unknown stays None, never 0."""

from decimal import Decimal as D

import pytest

from audr.api.llm import AssetMove, compute_asset_move, movers_to_markdown

pytestmark = pytest.mark.unit


def test_gain_pct_and_usd_use_current_quantity() -> None:
    m = compute_asset_move(price_now=D("110"), quantity=D("10"), price_ref=D("100"))
    assert m == AssetMove(D("10.00"), D("100.00"))


def test_loss_is_negative() -> None:
    m = compute_asset_move(price_now=D("90"), quantity=D("2"), price_ref=D("100"))
    assert m == AssetMove(D("-10.00"), D("-20.00"))


def test_missing_history_is_unknown_not_zero() -> None:
    assert compute_asset_move(price_now=D("5"), quantity=D("1"), price_ref=None) == AssetMove(
        None, None
    )


def test_unpriced_or_zero_reference_is_unknown() -> None:
    assert compute_asset_move(price_now=None, quantity=D("1"), price_ref=D("1")).pct is None
    assert compute_asset_move(price_now=D("1"), quantity=D("1"), price_ref=D("0")).pct is None


def test_unknown_quantity_keeps_pct_drops_usd() -> None:
    m = compute_asset_move(price_now=D("110"), quantity=None, price_ref=D("100"))
    assert m == AssetMove(D("10.00"), None)


def test_markdown_orders_by_abs_usd_unknown_last() -> None:
    unk = AssetMove(None, None)
    rows = [
        {"symbol": "U", "price_usd": None, "value_usd": None, "24h": unk, "7d": unk},
        {
            "symbol": "A",
            "price_usd": "1",
            "value_usd": "1",
            "24h": AssetMove(D("5"), D("1")),
            "7d": unk,
        },
        {
            "symbol": "B",
            "price_usd": "1",
            "value_usd": "1",
            "24h": AssetMove(D("-1"), D("-9")),
            "7d": unk,
        },
    ]
    md = movers_to_markdown(rows, [])
    assert md.index("- B:") < md.index("- A:") < md.index("- U:")
