"""POST /api/v1/assistant/chat and /conversations* — the stored-LLM assistant (AUD-955, AUD-958).

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

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.api.llm import (
    _asset_to_markdown,
    _context_to_markdown,
    build_llm_asset,
    build_llm_context,
)
from audr.assistant.presets import PRESETS, PRESETS_BY_ID, Preset
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
    mode: Literal["portfolio", "news", "chat"] | None = Field(
        default=None,
        description=(
            "Which context to attach: portfolio snapshot, news-heavy, or general chat. "
            "Required unless `preset` is given (the preset then decides)."
        ),
    )
    preset: str | None = Field(
        default=None,
        description="A preset id from GET /assistant/presets; its prompt is the user turn.",
    )
    asset_ref: str | None = Field(
        default=None,
        description="Symbol, contract address or asset id; required by presets with needs_asset.",
    )
    messages: list[ChatMessage] = Field(
        default_factory=list,
        description=(
            "Without `conversation_id`: the conversation so far, oldest first (stored as a "
            "new conversation). With `conversation_id`: only the LAST message is used — the "
            "new user turn; earlier turns come from the server. Only the last 10 turns reach "
            "the model. May be empty for `portfolio`/`news` without `conversation_id`; the "
            "last turn must be `user`."
        ),
    )
    conversation_id: UUID | None = Field(
        default=None,
        description="Continue this stored conversation. Omit to start a new one.",
    )


class PresetOut(BaseModel):
    id: str
    label: str
    description: str
    needs_asset: bool = Field(description="True when `asset_ref` is required in /chat.")


class UsageOut(BaseModel):
    input_tokens: int = Field(description="Prompt tokens the provider reported; 0 if unreported.")
    output_tokens: int = Field(description="Completion tokens reported; 0 if unreported.")


class AssistantChatResponse(BaseModel):
    reply: str = Field(description="The model's answer, reasoning blocks removed.")
    mode: Literal["portfolio", "news", "chat"] = Field(
        description="Context mode used (a preset's mode; `asset` presets report `chat`)."
    )
    provider: str = Field(description="Configured provider: anthropic or openai_compatible.")
    model: str = Field(description="Model that answered.")
    context_as_of: str = Field(description="RFC3339 time the attached context was built.")
    usage: UsageOut = Field(description="Token usage for this call.")
    elapsed_ms: int = Field(description="Wall time of the whole request in milliseconds.")
    conversation_id: UUID = Field(description="The stored conversation this turn belongs to.")
    message_id: UUID = Field(description="The stored assistant message holding this reply.")


def _prepare_turns(
    mode: str, messages: list[ChatMessage], preset: Preset | None = None
) -> list[dict[str, str]]:
    turns = [{"role": m.role, "content": m.content} for m in messages[-MAX_TURNS:]]
    # Providers want the conversation to open with the user.
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if not turns:
        if preset is not None:
            return [{"role": "user", "content": preset.user_prompt}]
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


async def _recent_headlines(db: AsyncSession, limit: int) -> list[dict[str, Any]]:
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


def _headlines_markdown(rows: list[dict[str, Any]]) -> str:
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


_IMPACT_FULL_MAX = 10
_IMPACT_MIN_IMPORTANCE = 60


def _strip_headlines_and_alerts(context_md: str) -> str:
    """Drop the generic '## Headlines' and '## Alerts' blocks (everything up to Freshness)."""
    start = context_md.find("## Headlines")
    end = context_md.find("## Freshness")
    if start == -1 or end == -1 or end < start:
        return context_md
    return context_md[:start] + context_md[end:]


def _impact_headlines_markdown(rows: list[dict[str, Any]]) -> str:
    """news_impact context: up to 10 important headlines in full, the rest title-only."""
    important = sorted(
        (r for r in rows if (r["importance"] or -1) >= _IMPACT_MIN_IMPORTANCE),
        key=lambda r: (-r["importance"], -r["published_at"].timestamp()),
    )
    full = important[:_IMPACT_FULL_MAX]
    full_ids = {id(r) for r in full}
    rest = [r for r in rows if id(r) not in full_ids]
    lines = [f"## Headlines to assess (importance >= {_IMPACT_MIN_IMPORTANCE}, {len(full)})"]
    if not full:
        lines.append("- (none)")
    for r in full:
        lines.append(
            f"- [{r['symbol']}] {r['title']} — {r['news_site']}, "
            f"{r['published_at'].isoformat()} (importance {r['importance']}/100)"
        )
    lines += ["", f"## Ignored headlines — titles only ({len(rest)})"]
    if not rest:
        lines.append("- (none)")
    for r in rest:
        lines.append(f"- [{r['symbol']}] {r['title']}")
    return "\n".join(lines) + "\n"


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _config_to_client(config: LlmConfig) -> LlmClient:
    settings = get_settings()
    prices = None
    if config.price_input_per_mtok is not None or config.price_output_per_mtok is not None:
        prices = (
            config.price_input_per_mtok or Decimal(0),
            config.price_output_per_mtok or Decimal(0),
        )
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
            "The stored LLM base URL no longer passes the host policy; "
            "re-save it under Connections.",
        ) from exc
    return _build_client_or_409(config)


_TITLE_CHARS = 60
_QUICK_TITLE = {"portfolio": "Analyse my portfolio", "news": "What matters in the news"}


def _stale_after_s() -> float:
    st = get_settings()
    return st.assistant_timeout_s * (st.assistant_retries + 1) + 60


def _merge_same_role(turns: list[ChatMessage]) -> list[ChatMessage]:
    """Join adjacent same-role turns (a failed answer leaves two user turns in a row)."""
    out: list[ChatMessage] = []
    for t in turns:
        if out and out[-1].role == t.role:
            out[-1] = ChatMessage(role=t.role, content=out[-1].content + "\n\n" + t.content)
        else:
            out.append(t)
    return out


async def _sweep_stale(db: AsyncSession) -> None:
    await db.execute(
        sa.text(
            """
            UPDATE assistant_message
               SET status = 'error', error_code = 'abandoned'
             WHERE status = 'pending'
               AND created_at < now() - make_interval(secs => :secs)
            """
        ),
        {"secs": _stale_after_s()},
    )
    await db.commit()


async def _finish(
    db: AsyncSession,
    conversation_id: UUID,
    message_id: UUID,
    *,
    status: str,
    content: str = "",
    error_code: str | None = None,
    model: str | None = None,
    context_as_of: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    elapsed_ms: int | None = None,
) -> None:
    """Settle the pending assistant row in its own transaction."""
    await db.rollback()
    await db.execute(
        sa.text(
            """
            UPDATE assistant_message
               SET status = :status, content = :content, error_code = :error_code,
                   model = :model, context_as_of = :context_as_of,
                   input_tokens = :input_tokens, output_tokens = :output_tokens,
                   elapsed_ms = :elapsed_ms
             WHERE id = :id
            """
        ),
        {
            "id": message_id,
            "status": status,
            "content": content,
            "error_code": error_code,
            "model": model,
            "context_as_of": context_as_of,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "elapsed_ms": elapsed_ms,
        },
    )
    await db.execute(
        sa.text("UPDATE assistant_conversation SET updated_at = now() WHERE id = :c"),
        {"c": conversation_id},
    )
    await db.commit()


async def _history(db: AsyncSession, conversation_id: UUID) -> list[ChatMessage]:
    rows = await db.execute(
        sa.text(
            """
            SELECT role, content FROM assistant_message
             WHERE conversation_id = :c AND (role = 'user' OR status = 'done')
             ORDER BY seq
            """
        ),
        {"c": conversation_id},
    )
    return [ChatMessage(role=r["role"], content=r["content"]) for r in rows.mappings()]


@router.get("/presets", response_model=list[PresetOut])
async def list_presets(_session: Annotated[Session, Depends(_require_session)]) -> list[PresetOut]:
    return [
        PresetOut(id=p.id, label=p.label, description=p.description, needs_asset=p.needs_asset)
        for p in PRESETS
    ]


def _resolve_preset(body: AssistantChatRequest) -> tuple[Preset | None, str]:
    """Validate preset/mode before anything is stored; returns (preset, context mode)."""
    preset: Preset | None = None
    if body.preset is not None:
        preset = PRESETS_BY_ID.get(body.preset)
        if preset is None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown preset: {body.preset[:40]}",
            )
        if preset.needs_asset and not (body.asset_ref and body.asset_ref.strip()):
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"preset {preset.id} needs asset_ref",
            )
        return preset, preset.mode
    if body.mode is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="either mode or preset is required",
        )
    return None, body.mode


@router.post("/chat", response_model=AssistantChatResponse)
async def assistant_chat(
    body: AssistantChatRequest,
    session: Annotated[Session, Depends(_require_csrf)],
    client: Annotated[LlmClient, Depends(get_assistant_client)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssistantChatResponse:
    started = time.monotonic()
    try:
        preset, ctx_mode = _resolve_preset(body)
        asset_md = ""
        if preset is not None and preset.mode == "asset":
            # Resolve before anything is stored: unknown asset -> 404, nothing persisted.
            asset_body = await build_llm_asset(db, session, str(body.asset_ref))
            if asset_body is None:
                raise _error(http_status.HTTP_404_NOT_FOUND, "not_found", "Asset not found.")
            asset_md = _asset_to_markdown(asset_body)
    except BaseException:
        await client.close()
        raise
    # "asset" presets are stored/reported as chat conversations.
    mode: Literal["portfolio", "news", "chat"] = "chat" if ctx_mode == "asset" else ctx_mode  # type: ignore[assignment]
    conversation_id = body.conversation_id
    try:
        if conversation_id is not None:
            exists = await db.scalar(
                sa.text("SELECT 1 FROM assistant_conversation WHERE id = :c"),
                {"c": conversation_id},
            )
            if not exists:
                raise _error(http_status.HTTP_404_NOT_FOUND, "not_found", "Conversation not found.")
            if not body.messages:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="a continued conversation needs a new user message",
                )
            new_turns = body.messages[-1:]
            all_turns = [*await _history(db, conversation_id), *new_turns]
        else:
            new_turns = list(body.messages)
            all_turns = list(body.messages)
        turns = _prepare_turns(mode, _merge_same_role(all_turns), preset)
        if conversation_id is None and not new_turns:
            new_turns = [ChatMessage(role="user", content=turns[-1]["content"])]

        # Durable BEFORE the LLM call: the user turn and a pending assistant row.
        if conversation_id is None:
            first_user = next((t.content for t in new_turns if t.role == "user"), "")
            title = (
                " ".join(first_user.split())[:_TITLE_CHARS]
                if body.messages
                else (preset.label if preset else _QUICK_TITLE.get(mode, ""))
            ) or "New chat"
            conversation_id = await db.scalar(
                sa.text(
                    "INSERT INTO assistant_conversation (title, mode) VALUES (:t, :m) RETURNING id"
                ),
                {"t": title, "m": mode},
            )
        for t in new_turns:
            await db.execute(
                sa.text(
                    "INSERT INTO assistant_message (conversation_id, role, content, status) "
                    "VALUES (:c, :r, :ct, 'done')"
                ),
                {"c": conversation_id, "r": t.role, "ct": t.content},
            )
        message_id = await db.scalar(
            sa.text(
                "INSERT INTO assistant_message (conversation_id, role, status) "
                "VALUES (:c, 'assistant', 'pending') RETURNING id"
            ),
            {"c": conversation_id},
        )
        await db.execute(
            sa.text("UPDATE assistant_conversation SET updated_at = now() WHERE id = :c"),
            {"c": conversation_id},
        )
        await db.commit()
    except BaseException:
        await client.close()
        raise

    if conversation_id is None or message_id is None:  # unreachable: both set above
        raise RuntimeError("conversation was not stored")
    try:
        headlines = {"portfolio": 5, "news": 5, "chat": _CHAT_HEADLINES}[mode]
        alerts = _NEWS_ALERTS if mode == "news" else 5
        context = await build_llm_context(
            db, session, headlines_limit=headlines, alerts_limit=alerts
        )
        context_md = _context_to_markdown(context)
        if mode == "news" and preset is not None and preset.trim_news_context:
            context_md = _strip_headlines_and_alerts(context_md)
            context_md += "\n" + _impact_headlines_markdown(
                await _recent_headlines(db, _NEWS_HEADLINES)
            )
        elif mode == "news":
            context_md += "\n" + _headlines_markdown(await _recent_headlines(db, _NEWS_HEADLINES))
        if asset_md:
            context_md += "\n" + asset_md
        if preset is not None:
            context_md += preset.system_suffix

        try:
            result: ChatResult = await client.chat(
                system=_SYSTEM_PROMPT + context_md,
                messages=turns,
                max_tokens=max(
                    get_settings().assistant_max_output_tokens,
                    preset.min_output_tokens or 0 if preset else 0,
                ),
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
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.shield(
                _finish(db, conversation_id, message_id, status="error", error_code="cancelled")
            )
        raise
    except BaseException as exc:
        detail: Any = exc.detail if isinstance(exc, HTTPException) else None
        code = detail["code"] if isinstance(detail, dict) else "internal_error"
        with contextlib.suppress(Exception):
            await _finish(db, conversation_id, message_id, status="error", error_code=code)
        raise
    finally:
        await client.close()

    elapsed_ms = int((time.monotonic() - started) * 1000)
    await _finish(
        db,
        conversation_id,
        message_id,
        status="done",
        content=result.text,
        model=result.model,
        context_as_of=context.as_of,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        elapsed_ms=elapsed_ms,
    )
    provider = "anthropic" if type(client).__name__ == "AnthropicClient" else "openai_compatible"
    return AssistantChatResponse(
        reply=result.text,
        mode=mode,
        provider=provider,
        model=result.model,
        context_as_of=context.as_of,
        usage=UsageOut(input_tokens=result.input_tokens, output_tokens=result.output_tokens),
        elapsed_ms=elapsed_ms,
        conversation_id=conversation_id,
        message_id=message_id,
    )


# --- stored conversations (AUD-958) -----------------------------------------


class ConversationSummary(BaseModel):
    id: UUID
    title: str
    mode: Literal["portfolio", "news", "chat"]
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(description="Stored turns, including pending/error rows.")


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class StoredMessage(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    status: Literal["pending", "done", "error"] = Field(
        description="`pending` = the model is still answering; `error` carries `error_code`."
    )
    error_code: str | None = None
    model: str | None = None
    context_as_of: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_ms: int | None = None
    created_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[StoredMessage]


class RenameRequest(BaseModel):
    title: str = Field(description="New title, 1-120 characters after trimming.")

    @field_validator("title")
    @classmethod
    def _len(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= 120:
            raise ValueError("title must be 1-120 characters")
        return v


_SUMMARY_SQL = """
    SELECT c.id, c.title, c.mode, c.created_at, c.updated_at,
           (SELECT count(*) FROM assistant_message m WHERE m.conversation_id = c.id)
             AS message_count
      FROM assistant_conversation c
"""


async def _get_summary(db: AsyncSession, conversation_id: UUID) -> dict[str, Any]:
    row = (
        (await db.execute(sa.text(_SUMMARY_SQL + " WHERE c.id = :c"), {"c": conversation_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise _error(http_status.HTTP_404_NOT_FOUND, "not_found", "Conversation not found.")
    return dict(row)


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    _session: Annotated[Session, Depends(_require_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationList:
    await _sweep_stale(db)
    total = await db.scalar(sa.text("SELECT count(*) FROM assistant_conversation"))
    rows = await db.execute(
        sa.text(_SUMMARY_SQL + " ORDER BY c.updated_at DESC, c.id LIMIT :l OFFSET :o"),
        {"l": limit, "o": offset},
    )
    return ConversationList(
        items=[ConversationSummary(**dict(r)) for r in rows.mappings()],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID,
    _session: Annotated[Session, Depends(_require_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationDetail:
    await _sweep_stale(db)
    summary = await _get_summary(db, conversation_id)
    rows = await db.execute(
        sa.text(
            """
            SELECT id, role, content, status, error_code, model, context_as_of,
                   input_tokens, output_tokens, elapsed_ms, created_at
              FROM assistant_message WHERE conversation_id = :c ORDER BY seq
            """
        ),
        {"c": conversation_id},
    )
    return ConversationDetail(
        **summary, messages=[StoredMessage(**dict(r)) for r in rows.mappings()]
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummary)
async def rename_conversation(
    conversation_id: UUID,
    body: RenameRequest,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationSummary:
    res = await db.execute(
        sa.text("UPDATE assistant_conversation SET title = :t WHERE id = :c"),
        {"t": body.title, "c": conversation_id},
    )
    if res.rowcount == 0:  # type: ignore[attr-defined]
        raise _error(http_status.HTTP_404_NOT_FOUND, "not_found", "Conversation not found.")
    await db.commit()
    return ConversationSummary(**await _get_summary(db, conversation_id))


@router.delete("/conversations/{conversation_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    res = await db.execute(
        sa.text("DELETE FROM assistant_conversation WHERE id = :c"), {"c": conversation_id}
    )
    if res.rowcount == 0:  # type: ignore[attr-defined]
        raise _error(http_status.HTTP_404_NOT_FOUND, "not_found", "Conversation not found.")
    await db.commit()


@router.delete("/conversations", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_all_conversations(
    _session: Annotated[Session, Depends(_require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await db.execute(sa.text("DELETE FROM assistant_conversation"))
    await db.commit()
