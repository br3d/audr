"""Unit tests for environment parsing and startup validation."""

import pytest
from pydantic import ValidationError

from audr.config import Settings


@pytest.mark.unit
def test_valid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    settings = Settings()
    assert settings.database_url == "postgresql+psycopg://user:pass@localhost/audr"
    assert settings.secret_key.get_secret_value() == "super-secret-key"
    assert settings.debug is False
    assert settings.log_level == "INFO"


@pytest.mark.unit
def test_missing_database_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    errors = exc_info.value.errors()
    fields = {e["loc"][0] for e in errors}
    assert "database_url" in fields


@pytest.mark.unit
def test_missing_secret_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    errors = exc_info.value.errors()
    fields = {e["loc"][0] for e in errors}
    assert "secret_key" in fields


@pytest.mark.unit
def test_invalid_database_url_scheme_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    errors = exc_info.value.errors()
    messages = " ".join(str(e["msg"]) for e in errors)
    assert "postgresql+psycopg://" in messages


@pytest.mark.unit
def test_debug_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.delenv("DEBUG", raising=False)
    settings = Settings()
    assert settings.debug is False


@pytest.mark.unit
def test_debug_can_be_set_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.setenv("DEBUG", "true")
    settings = Settings()
    assert settings.debug is True


@pytest.mark.unit
def test_rpc_rate_limit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.delenv("RPC_RATE_LIMIT_PER_SECOND", raising=False)
    monkeypatch.delenv("RPC_RATE_LIMIT_BURST", raising=False)
    monkeypatch.delenv("EVENT_INDEXER_MAX_CHUNKS_PER_RUN", raising=False)
    monkeypatch.delenv("EVENT_INDEXER_BACKFILL_BLOCKS", raising=False)
    settings = Settings()
    assert settings.rpc_rate_limit_per_second == 10.0
    assert settings.rpc_rate_limit_burst == 5
    assert settings.event_indexer_max_chunks_per_run == 50
    assert settings.event_indexer_backfill_blocks == 100_000


@pytest.mark.unit
def test_rpc_rate_limit_overridable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.setenv("RPC_RATE_LIMIT_PER_SECOND", "3.5")
    monkeypatch.setenv("RPC_RATE_LIMIT_BURST", "2")
    monkeypatch.setenv("EVENT_INDEXER_MAX_CHUNKS_PER_RUN", "7")
    monkeypatch.setenv("EVENT_INDEXER_BACKFILL_BLOCKS", "0")
    settings = Settings()
    assert settings.rpc_rate_limit_per_second == 3.5
    assert settings.rpc_rate_limit_burst == 2
    assert settings.event_indexer_max_chunks_per_run == 7
    assert settings.event_indexer_backfill_blocks == 0


@pytest.mark.unit
def test_cmc_rate_limit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.delenv("CMC_RATE_LIMIT_PER_SECOND", raising=False)
    monkeypatch.delenv("CMC_RATE_LIMIT_BURST", raising=False)
    settings = Settings()
    assert settings.cmc_rate_limit_per_second == 0.5
    assert settings.cmc_rate_limit_burst == 1


@pytest.mark.unit
def test_cmc_rate_limit_overridable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.setenv("CMC_RATE_LIMIT_PER_SECOND", "0.2")
    monkeypatch.setenv("CMC_RATE_LIMIT_BURST", "2")
    settings = Settings()
    assert settings.cmc_rate_limit_per_second == 0.2
    assert settings.cmc_rate_limit_burst == 2


@pytest.mark.unit
def test_asset_icons_remote_fetch_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.delenv("ASSET_ICONS_REMOTE_FETCH", raising=False)
    settings = Settings()
    assert settings.asset_icons_remote_fetch is True
    assert settings.asset_icon_cg_rate_limit_per_second == 0.5
    assert settings.asset_icon_cg_rate_limit_burst == 1


@pytest.mark.unit
def test_asset_icons_remote_fetch_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.setenv("ASSET_ICONS_REMOTE_FETCH", "false")
    settings = Settings()
    assert settings.asset_icons_remote_fetch is False
