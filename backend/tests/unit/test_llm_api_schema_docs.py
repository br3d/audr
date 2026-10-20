"""Every /api/v1/llm/* field documents what null and as_of mean (AUD-633 AC).

A model handed a bare `null` or a bare `as_of` timestamp with no explanation
is exactly the failure mode docs/llm-access.md §3 calls out: it will present
an unpriced asset as worth zero, or a three-hour-stale figure as live. These
tests assert every field on every LLM response schema carries a non-empty
OpenAPI `description`, generated from `Field(description=...)` on the
Pydantic models in `audr.api.llm`.
"""

from __future__ import annotations

import pytest

from audr.api.app import app

pytestmark = pytest.mark.unit

# Every Pydantic model audr.api.llm defines, from the request docstring down
# to the nested blocks — kept in sync by test_all_llm_schemas_are_covered.
_LLM_SCHEMA_NAMES = (
    "PortfolioBlockOut",
    "TopAllocationOut",
    "DeltasOut",
    "HeadlineOut",
    "AlertSummaryOut",
    "AlertsBlockOut",
    "FreshnessOut",
    "LlmContextResponse",
    "AssetHoldingOut",
    "AssetSeriesPointOut",
    "AssetNewsItemOut",
    "AssetAlertItemOut",
    "AssetAlertsBlockOut",
    "LlmAssetResponse",
)


@pytest.fixture(scope="module")
def schemas() -> dict:
    return app.openapi()["components"]["schemas"]


def test_all_llm_schemas_are_covered(schemas: dict) -> None:
    from pydantic import BaseModel

    import audr.api.llm as llm_module

    for name in dir(llm_module):
        obj = getattr(llm_module, name)
        is_llm_owned_model = (
            isinstance(obj, type)
            and issubclass(obj, BaseModel)
            and obj.__module__ == llm_module.__name__
        )
        if is_llm_owned_model and name in schemas:
            assert name in _LLM_SCHEMA_NAMES, (
                f"{name!r} is a new audr.api.llm schema not listed in "
                "_LLM_SCHEMA_NAMES above — add it so its fields get checked too"
            )


@pytest.mark.parametrize("schema_name", _LLM_SCHEMA_NAMES)
def test_every_field_has_a_description(schemas: dict, schema_name: str) -> None:
    schema = schemas[schema_name]
    properties = schema.get("properties", {})
    assert properties, f"{schema_name} has no properties to document"
    for field_name, field_schema in properties.items():
        description = field_schema.get("description")
        assert description, f"{schema_name}.{field_name} has no OpenAPI description"


def test_as_of_fields_explain_what_moment_they_describe(schemas: dict) -> None:
    for schema_name in _LLM_SCHEMA_NAMES:
        properties = schemas[schema_name].get("properties", {})
        if "as_of" in properties:
            description = properties["as_of"]["description"].lower()
            moment_words = ("stale", "snapshot", "observ", "recent", "newest", "generated")
            assert any(word in description for word in moment_words), (
                f"{schema_name}.as_of's description doesn't explain what moment "
                f"it refers to: {description!r}"
            )


def test_nullable_fields_explain_that_null_is_not_zero_or_unknown(schemas: dict) -> None:
    for schema_name in _LLM_SCHEMA_NAMES:
        properties = schemas[schema_name].get("properties", {})
        for field_name, field_schema in properties.items():
            any_of = field_schema.get("anyOf", [])
            is_nullable = any(branch.get("type") == "null" for branch in any_of)
            if not is_nullable:
                continue
            description = field_schema["description"].lower()
            assert "null" in description or "unknown" in description, (
                f"{schema_name}.{field_name} is nullable but its description "
                f"doesn't say what null means: {description!r}"
            )
