"""A curated OpenAPI document for non-session clients (AUD-633).

FastAPI's auto-generated schema describes every route, including the
session+CSRF-only operator surfaces (`/integrations`, `/settings*`,
`/jobs*`, `/data/*`, `/exports/*`) that `audr.api.auth` deliberately keeps
off the API-token allow-list. Publishing that whole document at
`GET /openapi.json` — unauthenticated, since it carries no portfolio data —
would hand anyone who can reach the host a complete map of routes they can
never legally call, which is a reconnaissance gift, not a convenience.

`build_curated_openapi()` re-uses FastAPI's own schema generation, so types
and `$ref`s stay correct as routes evolve, but keeps only the GET operations
whose path `audr.api.auth._is_token_allowed_path` already governs for a live
request — one source of truth for "what can a non-session client see",
checked against drift by `tests/unit/test_openapi_curated.py`.
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI

from audr.api.auth import _is_token_allowed_path

_ALLOWED_METHODS = frozenset({"get"})

_DESCRIPTION = (
    "Read-only surface for automation and LLM clients. Every route here can "
    "also be reached with an `Authorization: Bearer audr_pat_...` API token "
    "(minted from Settings → API Tokens) instead of a browser session; "
    "tokens are GET/HEAD-only and limited to exactly the routes listed below "
    "— nothing under /integrations, /settings, /jobs, /data or /exports "
    "ever appears here, by construction.\n\n"
    "Figures reflect the last published snapshot or cache, not a live read "
    "of the chain or any provider, and can be stale. Monetary and quantity "
    "fields are exact decimal strings, never JSON floats, and `null` always "
    "means unknown — never zero. See GET /.well-known/llms.txt for a "
    "short plain-text version of this notice."
)


def build_curated_openapi(app: FastAPI) -> dict[str, Any]:
    """Filter *app*'s generated schema down to allow-listed GET operations."""
    full = copy.deepcopy(app.openapi())

    paths: dict[str, Any] = {}
    for path, path_item in full.get("paths", {}).items():
        if not _is_token_allowed_path(path):
            continue
        kept_methods = {
            method: operation
            for method, operation in path_item.items()
            if method in _ALLOWED_METHODS
        }
        if kept_methods:
            paths[path] = kept_methods

    all_schemas = full.get("components", {}).get("schemas", {})
    kept_schemas = _collect_referenced_schemas(paths, all_schemas)

    return {
        "openapi": full["openapi"],
        "info": {
            "title": "audr — read-only API for automation and LLM clients",
            "version": full["info"]["version"],
            "description": _DESCRIPTION,
        },
        "paths": paths,
        "components": {
            "schemas": kept_schemas,
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "An audr_pat_... read-only API token. Every route in this "
                        "document is already read-only, so the bearer token needs "
                        "no additional scope."
                    ),
                }
            },
        },
        "security": [{"bearerAuth": []}],
    }


def _collect_referenced_schemas(
    paths: dict[str, Any], all_schemas: dict[str, Any]
) -> dict[str, Any]:
    """Transitive closure of `$ref` schema names reachable from *paths*."""
    keep: set[str] = set()
    pending = _ref_names_in(paths)
    while pending:
        name = pending.pop()
        if name in keep or name not in all_schemas:
            continue
        keep.add(name)
        pending |= _ref_names_in(all_schemas[name])
    return {name: all_schemas[name] for name in sorted(keep)}


def _ref_names_in(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= _ref_names_in(value)
    elif isinstance(node, list):
        for item in node:
            found |= _ref_names_in(item)
    return found
