"""`GET /.well-known/llms.txt` — a public pointer for LLM/automation clients (AUD-633).

Contains no portfolio data — only what this service is, how to read it, and
how to authenticate — so it is served unauthenticated on purpose, same as
the curated `GET /openapi.json` it points to.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

router = APIRouter()

LLMS_TXT = """\
# audr

A single-owner, self-hosted Ethereum portfolio tracker. This instance is
private: it is not a public API, and reaching it at all usually means you
already have a relationship with its owner.

## Reading this instance

- Context-window-shaped read surface: GET /api/v1/llm/context (one snapshot
  of the whole portfolio) and GET /api/v1/llm/asset/{ref} (one asset, by
  symbol, contract address, or UUID).
- Full machine-readable schema: GET /openapi.json — curated. It lists only
  the read-only routes below; mutating and operator routes never appear
  there.
- Also readable: /api/v1/portfolio, /api/v1/holdings, /api/v1/assets,
  /api/v1/history, /api/v1/events, /api/v1/allowances, /api/v1/news,
  /api/v1/alerts, /api/v1/status, /api/v1/version.

## Authentication

Send `Authorization: Bearer audr_pat_...`. The owner mints this token from
Settings -> API Tokens in the web UI; it is shown once and is read-only by
construction (GET/HEAD only, and only for the routes listed above).

## Before you answer anything with this data

- Figures reflect the last published snapshot or cache, not a live read of
  the chain or any provider. They can be stale.
- `total_usd`, and any other numeric field, can be `null`. Null means
  unknown, never zero: an unpriced asset or an unscanned wallet is not a
  zero balance.
- Every /api/v1/llm/* response carries `as_of` and a plain-English
  `caveats` array — read both before stating a number as fact.
"""


@router.get("/.well-known/llms.txt", include_in_schema=False)
async def get_llms_txt() -> Response:
    return Response(content=LLMS_TXT, media_type="text/plain; charset=utf-8")
