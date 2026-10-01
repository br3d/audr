"""Environment parsing and startup validation."""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    database_url: str
    secret_key: SecretStr
    debug: bool = False
    log_level: str = "INFO"

    # Shared RPC rate-limit budget for the worker process (AUD-362). Default
    # is conservative for Infura's free tier (10 req/s) with a small burst so
    # balance_scan, discovery, and event_indexer don't collectively trip 429s.
    rpc_rate_limit_per_second: float = 10.0
    rpc_rate_limit_burst: int = 5

    # Caps how many eth_getLogs block-chunks event_indexer processes in a
    # single run; the rest resume from the per-wallet checkpoint next run
    # (AUD-362) so one run can't balloon into an unbounded RPC burst.
    event_indexer_max_chunks_per_run: int = 50

    # Anonymous CoinMarketCap rate-limit budget for the worker process
    # (AUD-370). The keyless public API's unpublished per-IP quota is far
    # tighter than any RPC provider's — quote_refresh kept tripping HTTP 429
    # even at one run per hour once a portfolio's chunked id requests outran
    # the ad-hoc inter-batch sleep that preceded this setting.
    cmc_rate_limit_per_second: float = 0.5
    cmc_rate_limit_burst: int = 1

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use the postgresql+psycopg:// scheme")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
