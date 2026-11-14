"""On-demand integration validation handlers — VALIDATE_RPC / VALIDATE_QUOTES /
VALIDATE_LLM (AUD-313, AUD-743).

These jobs are triggered interactively from the Connections settings page
(POST /api/v1/integrations/{kind}/validate).  Each handler probes the stored
integration with a live, read-only request.  A handler that returns normally
marks the job ``completed`` (health → ok); a handler that raises marks it
``failed`` with the exception message (health → error, surfaced in the UI).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.policy import get_shared_rpc_rate_limiter
from audr.providers.coingecko_demo import CoinGeckoProvider
from audr.providers.llm import build_client
from audr.providers.rpc_reader import RpcReader
from audr.providers.rpc_targets import RpcUrlError, get_validated_rpc_url
from audr.settings.llm import PROVIDER_ANTHROPIC, load_validated_llm_config
from audr.settings.quotes import get_coingecko_api_key

logger = logging.getLogger(__name__)

_ETH_MAINNET_CHAIN_ID = 1


async def handle_validate_rpc(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Probe the configured RPC endpoint: chain-ID match + a live block read.

    Raises on any failure so the worker records a failed job with a
    human-readable error the UI can display.
    """
    rpc_url = await get_validated_rpc_url(session)
    if rpc_url is None:
        raise ValueError("No RPC endpoint configured.")

    async with RpcReader(
        url=rpc_url,
        expected_chain_id=_ETH_MAINNET_CHAIN_ID,
        rate_limiter=get_shared_rpc_rate_limiter(),
    ) as rpc:
        await rpc.validate_chain()
        block_number = await rpc.get_block_number()

    logger.info("validate_rpc ok run_id=%s block=%d", run_id, block_number)


async def handle_validate_quotes(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Probe the configured CoinGecko credentials with a single price read.

    Raises on any failure so the worker records a failed job with a
    human-readable error the UI can display.
    """
    api_key = await get_coingecko_api_key(session)
    if not api_key:
        raise ValueError("No CoinGecko API key configured.")

    async with CoinGeckoProvider(api_key=api_key) as provider:
        await provider.get_eth_price()

    logger.info("validate_quotes ok run_id=%s", run_id)


async def handle_validate_llm(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Probe the stored provider + model with a one-token call (AUD-743, AUD-932).

    Probes the same provider, base URL and model the gate will use, so "OK" here
    means the gate itself can run. Raises on any failure so the worker records
    the error for the UI — deliberately *not* fail-open, unlike the ingest gate
    this validates: the point of the button is to surface the failure the gate
    is designed to swallow.
    """
    try:
        config = await load_validated_llm_config(session)
    except RpcUrlError as exc:
        raise ValueError(f"LLM base URL rejected: {exc}") from exc
    if config is None:
        raise ValueError("No LLM provider configured.")
    if config.provider == PROVIDER_ANTHROPIC and not config.api_key:
        raise ValueError("No Anthropic API key configured.")

    client = build_client(
        provider=config.provider,
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        prices=None,
    )
    try:
        await client.check_connectivity()
    finally:
        await client.close()

    logger.info(
        "validate_llm ok run_id=%s provider=%s model=%s", run_id, config.provider, config.model
    )
