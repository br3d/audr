"""Versioned AES-256-GCM authenticated encryption for credentials and payloads.

Envelope format (v1):
  version (1 byte) | nonce (12 bytes) | ciphertext+GCM-tag

The 16-byte GCM authentication tag is appended to the ciphertext by AESGCM.encrypt.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION_1 = 1
_NONCE_LEN = 12
_TAG_LEN = 16
# version(1) + nonce(12) + tag(16) — minimum envelope for empty plaintext
_MIN_ENVELOPE_LEN = 1 + _NONCE_LEN + _TAG_LEN


class MissingKeyError(Exception):
    """Raised when the key is None or empty so callers can handle it distinctly."""


class InvalidEnvelopeError(Exception):
    """Raised for unknown version, truncated data, or failed GCM authentication."""


class EncryptedSecret:
    """Holds a ciphertext envelope without ever exposing it in repr/str."""

    __slots__ = ("_ciphertext",)

    def __init__(self, ciphertext: bytes) -> None:
        self._ciphertext = ciphertext

    @property
    def ciphertext(self) -> bytes:
        return self._ciphertext

    def __repr__(self) -> str:
        return "EncryptedSecret(<redacted>)"

    def __str__(self) -> str:
        return "EncryptedSecret(<redacted>)"


def encrypt(plaintext: bytes, aad: bytes, key: bytes | None) -> bytes:
    """Encrypt *plaintext* authenticated with *aad* under *key*.

    Returns a versioned envelope: version(1) || nonce(12) || ciphertext+tag.

    Raises:
        MissingKeyError: key is None or empty.
    """
    _require_key(key)
    assert key is not None  # noqa: S101 — narrowing only; _require_key already raised
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return bytes([_VERSION_1]) + nonce + ct


def decrypt(ciphertext: bytes, aad: bytes, key: bytes | None) -> bytes:
    """Decrypt a versioned envelope produced by :func:`encrypt`.

    Raises:
        MissingKeyError: key is None or empty.
        InvalidEnvelopeError: unknown version, truncated envelope, or auth failure.
    """
    _require_key(key)
    assert key is not None  # noqa: S101 — narrowing only; _require_key already raised
    if len(ciphertext) < _MIN_ENVELOPE_LEN:
        raise InvalidEnvelopeError(
            f"envelope is too short: {len(ciphertext)} bytes (minimum {_MIN_ENVELOPE_LEN})"
        )
    version = ciphertext[0]
    if version != _VERSION_1:
        raise InvalidEnvelopeError(f"Unknown version {version} in envelope")
    nonce = ciphertext[1 : 1 + _NONCE_LEN]
    body = ciphertext[1 + _NONCE_LEN :]
    try:
        return AESGCM(key).decrypt(nonce, body, aad)
    except InvalidTag as exc:
        raise InvalidEnvelopeError("authentication failed: wrong key or AAD") from exc


def _require_key(key: bytes | None) -> None:
    if key is None or len(key) == 0:
        raise MissingKeyError("master key is absent; call init_key() before encrypting")
