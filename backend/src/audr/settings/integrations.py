"""Encrypted RPC and quote-provider settings with revision checks (T029 / US1)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.crypto import decrypt, encrypt
from audr.operations.init_key import get_master_key


class RevisionConflictError(Exception):
    """Raised when the supplied revision does not match the current revision."""


@dataclass
class IntegrationRead:
    id: uuid.UUID
    kind: str
    revision: int
    created_at: datetime
    updated_at: datetime
    # Plaintext fields exposed only to the authenticated owner (never logged).
    url: str | None = None
    api_key: str | None = None
    allow_private_host: bool = False


async def get_integration(
    session: AsyncSession,
    *,
    kind: str,
    decrypt_fields: bool = False,
) -> IntegrationRead | None:
    """Read an integration row.  Set *decrypt_fields* to expose plaintext."""
    result = await session.execute(
        sa.text(
            "SELECT id, kind, revision, encrypted_blob, created_at, updated_at"
            " FROM integration WHERE kind = :kind"
        ),
        {"kind": kind},
    )
    row = result.first()
    if row is None:
        return None

    read = IntegrationRead(
        id=row[0],
        kind=row[1],
        revision=row[2],
        created_at=row[4],
        updated_at=row[5],
    )

    if decrypt_fields:
        key = await get_master_key(session)
        aad = f"integration:{kind}:{row[0]}".encode()
        payload = json.loads(decrypt(row[3], aad, key))
        read.url = payload.get("url")
        read.api_key = payload.get("api_key")
        read.allow_private_host = bool(payload.get("allow_private_host", False))

    return read


async def upsert_integration(
    session: AsyncSession,
    *,
    kind: str,
    url: str | None = None,
    api_key: str | None = None,
    allow_private_host: bool = False,
    expected_revision: int | None = None,
) -> IntegrationRead:
    """Create or replace an integration.

    If *expected_revision* is provided and does not match the stored revision,
    raises RevisionConflictError (optimistic lock).
    """
    key = await get_master_key(session)

    existing = await session.execute(
        sa.text("SELECT id, revision FROM integration WHERE kind = :kind"),
        {"kind": kind},
    )
    row = existing.first()

    if row is not None and expected_revision is not None:
        if row[1] != expected_revision:
            raise RevisionConflictError(
                f"revision mismatch: expected {expected_revision}, got {row[1]}"
            )

    payload = json.dumps(
        {"url": url, "api_key": api_key, "allow_private_host": allow_private_host}
    ).encode()

    if row is None:
        int_id = uuid.uuid4()
        revision = 1
        aad = f"integration:{kind}:{int_id}".encode()
        blob = encrypt(payload, aad, key)
        now = datetime.now(tz=UTC)
        await session.execute(
            sa.text(
                "INSERT INTO integration"
                " (id, kind, revision, encrypted_blob, created_at, updated_at)"
                " VALUES (:id, :kind, :rev, :blob, :now, :now)"
            ),
            {"id": int_id, "kind": kind, "rev": revision, "blob": blob, "now": now},
        )
    else:
        int_id = row[0]
        revision = row[1] + 1
        aad = f"integration:{kind}:{int_id}".encode()
        blob = encrypt(payload, aad, key)
        await session.execute(
            sa.text(
                "UPDATE integration SET revision = :rev, encrypted_blob = :blob, updated_at = now()"
                " WHERE id = :id"
            ),
            {"rev": revision, "blob": blob, "id": int_id},
        )

    await session.flush()
    result = await get_integration(session, kind=kind, decrypt_fields=False)
    assert result is not None  # noqa: S101
    return result
