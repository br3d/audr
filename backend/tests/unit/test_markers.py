"""Smoke tests verifying pytest marker registration."""

import pytest


@pytest.mark.unit
def test_unit_marker_recognized() -> None:
    assert True


@pytest.mark.integration
def test_integration_marker_recognized() -> None:
    pytest.skip("integration marker registered — skipped in unit run")


@pytest.mark.contract
def test_contract_marker_recognized() -> None:
    pytest.skip("contract marker registered — skipped in unit run")
