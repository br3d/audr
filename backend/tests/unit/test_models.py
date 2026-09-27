"""Unit tests for SQLAlchemy metadata base."""

import pytest
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from audr.models import Base, metadata


@pytest.mark.unit
def test_metadata_is_MetaData_instance() -> None:
    assert isinstance(metadata, MetaData)


@pytest.mark.unit
def test_base_is_declarative_base_subclass() -> None:
    assert issubclass(Base, DeclarativeBase)


@pytest.mark.unit
def test_base_uses_shared_metadata() -> None:
    assert Base.metadata is metadata


@pytest.mark.unit
def test_metadata_naming_convention_has_all_standard_keys() -> None:
    nc = metadata.naming_convention
    for key in ("ix", "uq", "ck", "fk", "pk"):
        assert key in nc, f"Missing naming convention key: {key}"


@pytest.mark.unit
def test_metadata_naming_convention_values_are_strings() -> None:
    nc = metadata.naming_convention
    for key, value in nc.items():
        assert isinstance(value, str), f"Naming convention {key!r} value must be a string"
