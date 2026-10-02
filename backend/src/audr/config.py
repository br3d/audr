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

    # Keyless asset icon cache (AUD-385): the worker resolves a token logo
    # once from Trust Wallet's GitHub asset repo or, failing that, CoinGecko's
    # keyless contract-lookup endpoint, and caches it in `asset_icon` so a
    # self-hosted install never leaks wallet holdings to a third-party CDN on
    # every dashboard render. An operator who does not want the backend
    # reaching out to GitHub/CoinGecko at all can turn this off; the UI then
    # degrades to generated monograms only.
    asset_icons_remote_fetch: bool = True

    # Rate limit for the keyless CoinGecko contract-lookup fallback used by
    # the asset icon cache (AUD-385) — mirrors cmc_rate_limit_per_second since
    # both hit unauthenticated CoinGecko/CoinMarketCap endpoints with tight,
    # unpublished per-IP quotas.
    asset_icon_cg_rate_limit_per_second: float = 0.5
    asset_icon_cg_rate_limit_burst: int = 1

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use the postgresql+psycopg:// scheme")
        return v


class SpaSettings(BaseSettings):
    """Knobs for serving the built SPA out of the API process (AUD-388).

    Deliberately a separate class rather than two more fields on `Settings`.
    `audr.api.app` builds the application at import time (module-level
    `app = create_app()`), and `Settings` has two *required* fields —
    `database_url` and `secret_key`. The test images intentionally start
    without `SECRET_KEY` (conftest injects it per-test via monkeypatch), so
    reading `Settings` during `create_app()` would turn importing the app into
    a hard dependency on a fully populated environment and break collection.

    Every field here has a default and nothing is required, so this class
    always constructs.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # On by default: since the nginx `web` container was removed, the API
    # process is the only thing serving the UI in a deployment. Dev is
    # unaffected because the mount is *also* conditional on the directory
    # existing, and a dev checkout has no `/app/static` — there the Vite dev
    # server serves the UI and proxies `/api`.
    serve_spa: bool = True

    # Where the `runtime` Dockerfile stage copies the Vite build to.
    spa_dir: str = "/app/static"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_spa_settings() -> SpaSettings:
    return SpaSettings()
