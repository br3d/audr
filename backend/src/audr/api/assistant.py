"""POST /api/v1/assistant/chat — on-demand analysis through the stored LLM (AUD-955).

Owner-only (session + CSRF); API tokens cannot reach it and it is not in the
curated /openapi.json. The request is synchronous and may take a minute with a
slow model: FastAPI serves the SPA itself, there is no proxy in front with a
60s cap, and it must never be routed through the job worker (jobs run one after
another there).

The context is the same snapshot as GET /api/v1/llm/context — built by the same
function, no second SQL layer — plus, per mode, more headlines (with the news
gate's `summary`/`importance` when present) and unread alerts. The client holds
the conversation; the server trims it and stores nothing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf
from audr.api.llm import _context_to_markdown, build_llm_context
from audr.auth.models import Session
from audr.config import get_settings
from audr.db import get_db
from audr.providers.llm import (
    ChatResult,
    LlmClient,
    LlmReasoningOnly,
    LlmTimeout,
    LlmUnavailable,
    build_client,
)
from audr.providers.rpc_targets import RpcUrlError
from audr.settings.llm import PROVIDER_ANTHROPIC, LlmConfig, load_validated_llm_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant")

MAX_TURNS = 10
MAX_CONTENT_CHARS = 4000

_NEWS_HEADLINES = 30
_CHAT_HEADLINES = 10
_NEWS_ALERTS = 20

_DEFAULT_REQUEST = {
    "portfolio": "Analyse my portfolio.",
    "news": "What in the latest news matters for my holdings?",
}

_SYSTEM_PROMPT = (
    "You are a read-only analyst for a single owner's self-hosted crypto portfolio. "
    "You cannot move funds or change anything, and nothing you say is financial advice.\n\n"
    "Rules:\n"
    "- Rely ONLY on the numbers and headlines in the context below. Do not invent "
    "prices, balances, or news. If a figure is null/unknown or the data is stale "
    "(see Freshness and Caveats), say so plainly — unknown is not zero.\n"
    "- Start with a short summary, then list points, each with its reasoning.\n"
    "- Answer in the language of the user's last message.\n\n"
    "=== CONTEXT ===\n"
)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = Field(description="Who wrote this turn.")
    content: str = Field(description="Turn text; truncated to 4000 characters server-side.")

    @field_validator("content")
    @classmethod
    def _cap(cls, v: str) -> str:
        return v[:MAX_CONTENT_CHARS]


class AssistantChatRequest(BaseModel):
    mode: Literal["portfolio", "news", "chat"] = Field(
        description="Which context to attach: portfolio snapshot, news-heavy, or general chat."
    )
    messages: list[ChatMessage] = Field(
        default_factory=list,
        description=(
            "Conversation so far, oldest first; the client owns it. Only the last 10 turns "
            "are used. May be empty for `portfolio`/`news`; the last turn must be `user`."
        ),
    )


class UsageOut(BaseModel):
    input_tokens: int = Field(description="Prompt tokens the provider reported; 0 if unreported.")
    output_tokens: int = Field(description="Completion tokens reported; 0 if unreported.")


class AssistantChatResponse(BaseModel):
    reply: str = Field(description="The model's answer, reasoning blocks removed.")
    mode: Literal["portfolio", "news", "chat"] = Field(description="Echo of the request mode.")
    provider: str = Field(description="Configured provider: anthropic or openai_compatible.")
    model: str = Field(description="Model that answered.")
    context_as_of: str = Field(description="RFC3339 time the attached context was built.")
    usage: UsageOut = Field(description="Token usage for this call.")
    elapsed_ms: int = Field(description="Wall time of the whole request in milliseconds.")


def _prepare_turns(mode: str, messages: list[ChatMessage]) -> list[dict[str, str]]:
    turns = [{"role": m.role, "content": m.content} for m in messages[-MAX_TURNS:]]
    # Providers want the conversation to open with the user.
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if not turns:
        if mode == "chat":
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="chat mode needs at least one user message",
            )
        return [{"role": "user", "content": _DEFAULT_REQUEST[mode]}]
    if turns[-1]["role"] != "user":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the last message must be from the user",
        )
    return turns


async def _recent_headlines(db: AsyncSession, limit: int) -> list[dict]:
    rows = await db.execute(
        sa.text(
            """
            SELECT a.symbol, an.title, an.news_site, an.published_at, an.summary, an.importance
            FROM asset_news an
            JOIN asset a ON a.id = an.asset_id
            ORDER BY an.published_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(r) for r in rows.mappings()]


def _headlines_markdown(rows: list[dict]) -> str:
    lines = [f"## Recent headlines with analysis ({len(rows)})"]
    if not rows:
        lines.append("- (none)")
    for r in rows:
        published: datetime = r["published_at"]
        line = f"- [{r['symbol']}] {r['title']} — {r['news_site']}, {published.isoformat()}"
        if r["importance"] is not None:
            line += f" (importance {r['importance']}/100)"
        if r["summary"]:
            line += f"\n  summary: {r['summary']}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _config_to_client(config: LlmConfig) -> LlmClient:
    settings = get_settings()
    prices = None
    if config.price_input_per_mtok is not None or config.price_output_per_mtok is not None:
        prices = (config.price_input_per_mtok or Decimal(0), config.price_output_per_mtok or Decimal(0))
    return build_client(
        provider=config.provider,
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        max_output_tokens=settings.assistant_max_output_tokens,
        prices=prices,
        timeout=settings.assistant_timeout_s,
        retries=settings.assistant_retries,
    )


def _build_client_or_409(config: LlmConfig | None) -> LlmClient:
    if config is None or (config.provider == PROVIDER_ANTHROPIC and not config.api_key):
        raise _error(
            http_status.HTTP_409_CONFLICT,
            "llm_not_configured",
            "No LLM provider is configured. Connect one under Connections.",
        )
    try:
        return _config_to_client(config)
    except LlmUnavailable as exc:
        raise _error(
            http_status.HTTP_409_CONFLICT,
            "llm_not_configured",
            f"The stored LLM configuration is unusable: {exc}",
        ) from exc


# Overridable in tests to inject a fake client.
async def get_assistant_client(db: Annotated[AsyncSession, Depends(get_db)]) -> LlmClient:
    try:
        config = await load_validated_llm_config(db)
    except RpcUrlError as exc:
        raise _error(
            http_status.HTTP_409_CONFLICT,
            "llm_not_configured",
            "The stored LLM base URL no longer passes the host policy; re-save it under Connections.",
        ) from exc
    return _build_client_or_409(config)


@router.post("/chat", response_model=AssistantChatResponse)
async def assistant_chat(
    body: AssistantChatRequest,
    session: Annotated[Session, Depends(_require_csrf)],
    client: Annotated[LlmClient, Depends(get_assistant_client)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssistantChatResponse:
    started = time.monotonic()
    turns = _prepare_turns(body.mode, body.messages)

    headlines = {"portfolio": 5, "news": 5, "chat": _CHAT_HEADLINES}[body.mode]
    alerts = _NEWS_ALERTS if body.mode == "news" else 5
    context = await build_llm_context(db, session, headlines_limit=headlines, alerts_limit=alerts)
    context_md = _context_to_markdown(context)
    if body.mode == "news":
        context_md += "\n" + _headlines_markdown(await _recent_headlines(db, _NEWS_HEADLINES))

    try:
        result: ChatResult = await client.chat(
            system=_SYSTEM_PROMPT + context_md,
            messages=turns,
            max_tokens=get_settings().assistant_max_output_tokens,
        )
    except LlmTimeout as exc:
        raise _error(
            http_status.HTTP_504_GATEWAY_TIMEOUT,
            "llm_timeout",
            "The model did not answer in time. Try again, or pick a faster model.",
        ) from exc
    except LlmReasoningOnly as exc:
        raise _error(http_status.HTTP_502_BAD_GATEWAY, "llm_reasoning_only", str(exc)) from exc
    except LlmUnavailable as exc:
        # str(exc) is built by the adapters from status codes/shape errors only;
        # the key is never in it.
        logger.warning("assistant chat failed: %s", type(exc).__name__)
        raise _error(
            http_status.HTTP_502_BAD_GATEWAY,
            "llm_error",
            f"The LLM provider failed: {exc}",
        ) from exc
    finally:
        await client.close()

    provider = "anthropic" if type(client).__name__ == "AnthropicClient" else "openai_compatible"
    return AssistantChatResponse(
        reply=result.text,
        mode=body.mode,
        provider=provider,
        model=result.model,
        context_as_of=context.as_of,
        usage=UsageOut(input_tokens=result.input_tokens, output_tokens=result.output_tokens),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
