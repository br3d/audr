"""Unit tests for AEAD envelope cryptography.

These tests are intentionally FAILING — they define the expected interface
for audr.operations.crypto before the implementation exists.

Covers:
  - Envelope versioning: first byte is version; unknown versions are rejected.
  - Record-bound AAD: ciphertexts cannot be used with a different record's AAD.
  - Missing-key recovery: None key raises MissingKeyError, not a cryptic error.
  - Secret redaction: EncryptedSecret never leaks plaintext or raw bytes in repr/str.
"""

import os

import pytest

from audr.operations.crypto import (
    EncryptedSecret,
    InvalidEnvelopeError,
    MissingKeyError,
    decrypt,
    encrypt,
)

_KEY = os.urandom(32)
_AAD = b"rpc_url:42"
_PLAINTEXT = b"https://mainnet.infura.io/v3/deadbeef"


class TestEnvelopeVersioning:
    """Envelope must carry a version byte so future algorithm upgrades can be detected."""

    @pytest.mark.unit
    def test_first_byte_is_version_one(self) -> None:
        ciphertext = encrypt(_PLAINTEXT, _AAD, _KEY)
        assert ciphertext[0] == 1

    @pytest.mark.unit
    def test_unknown_version_raises_invalid_envelope_error(self) -> None:
        ciphertext = encrypt(_PLAINTEXT, _AAD, _KEY)
        tampered = bytes([99]) + ciphertext[1:]
        with pytest.raises(InvalidEnvelopeError, match="[Uu]nknown version"):
            decrypt(tampered, _AAD, _KEY)

    @pytest.mark.unit
    def test_empty_envelope_raises_invalid_envelope_error(self) -> None:
        with pytest.raises(InvalidEnvelopeError):
            decrypt(b"", _AAD, _KEY)

    @pytest.mark.unit
    def test_truncated_envelope_missing_nonce_raises(self) -> None:
        # version byte present but nonce is incomplete (need 12 bytes minimum)
        with pytest.raises(InvalidEnvelopeError):
            decrypt(b"\x01" + b"\x00" * 5, _AAD, _KEY)

    @pytest.mark.unit
    def test_envelope_without_tag_raises(self) -> None:
        # version + full nonce but no ciphertext/tag
        with pytest.raises(InvalidEnvelopeError):
            decrypt(b"\x01" + b"\x00" * 12, _AAD, _KEY)


class TestRecordBoundAAD:
    """AAD binds the ciphertext to a specific record — cross-record reuse is rejected."""

    @pytest.mark.unit
    def test_correct_aad_decrypts_successfully(self) -> None:
        ct = encrypt(_PLAINTEXT, _AAD, _KEY)
        result = decrypt(ct, _AAD, _KEY)
        assert result == _PLAINTEXT

    @pytest.mark.unit
    def test_wrong_record_id_rejected(self) -> None:
        ct = encrypt(_PLAINTEXT, b"rpc_url:42", _KEY)
        with pytest.raises(InvalidEnvelopeError):
            decrypt(ct, b"rpc_url:99", _KEY)

    @pytest.mark.unit
    def test_wrong_record_type_rejected(self) -> None:
        ct = encrypt(_PLAINTEXT, b"rpc_url:42", _KEY)
        with pytest.raises(InvalidEnvelopeError):
            decrypt(ct, b"api_key:42", _KEY)

    @pytest.mark.unit
    def test_empty_aad_differs_from_non_empty_aad(self) -> None:
        ct = encrypt(_PLAINTEXT, b"rpc_url:42", _KEY)
        with pytest.raises(InvalidEnvelopeError):
            decrypt(ct, b"", _KEY)

    @pytest.mark.unit
    def test_non_empty_aad_differs_from_empty_aad(self) -> None:
        ct = encrypt(_PLAINTEXT, b"", _KEY)
        with pytest.raises(InvalidEnvelopeError):
            decrypt(ct, b"rpc_url:42", _KEY)


class TestMissingKeyRecovery:
    """When the master key is absent, operations raise MissingKeyError (not TypeError)."""

    @pytest.mark.unit
    def test_encrypt_with_none_key_raises_missing_key_error(self) -> None:
        with pytest.raises(MissingKeyError):
            encrypt(_PLAINTEXT, _AAD, None)  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_decrypt_with_none_key_raises_missing_key_error(self) -> None:
        ct = encrypt(_PLAINTEXT, _AAD, _KEY)
        with pytest.raises(MissingKeyError):
            decrypt(ct, _AAD, None)  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_missing_key_error_is_subclass_of_exception(self) -> None:
        assert issubclass(MissingKeyError, Exception)

    @pytest.mark.unit
    def test_missing_key_error_is_not_subclass_of_value_error(self) -> None:
        # Must be its own distinct error family so callers can handle it specifically.
        assert not issubclass(MissingKeyError, ValueError)

    @pytest.mark.unit
    def test_encrypt_with_empty_key_raises_missing_key_error(self) -> None:
        with pytest.raises(MissingKeyError):
            encrypt(_PLAINTEXT, _AAD, b"")


class TestSecretRedaction:
    """Encrypted values must never expose plaintext or raw key material in repr/str."""

    @pytest.mark.unit
    def test_encrypted_secret_repr_does_not_contain_plaintext(self) -> None:
        ct = encrypt(_PLAINTEXT, _AAD, _KEY)
        secret = EncryptedSecret(ct)
        assert _PLAINTEXT.decode() not in repr(secret)
        assert _PLAINTEXT.decode() not in str(secret)

    @pytest.mark.unit
    def test_encrypted_secret_repr_does_not_contain_raw_ciphertext_hex(self) -> None:
        ct = encrypt(_PLAINTEXT, _AAD, _KEY)
        secret = EncryptedSecret(ct)
        assert ct.hex() not in repr(secret)
        assert ct.hex() not in str(secret)

    @pytest.mark.unit
    def test_encrypted_secret_repr_contains_redacted_marker(self) -> None:
        ct = encrypt(_PLAINTEXT, _AAD, _KEY)
        secret = EncryptedSecret(ct)
        # repr must signal that the value is hidden, not just be empty
        assert "redacted" in repr(secret).lower()

    @pytest.mark.unit
    def test_encrypted_secret_ciphertext_property_returns_original_bytes(self) -> None:
        ct = encrypt(_PLAINTEXT, _AAD, _KEY)
        secret = EncryptedSecret(ct)
        assert secret.ciphertext == ct

    @pytest.mark.unit
    def test_wrong_key_error_message_does_not_expose_key_bytes(self) -> None:
        bad_key = b"\xba\xad\xf0\x0d" * 8
        ct = encrypt(_PLAINTEXT, _AAD, _KEY)
        try:
            decrypt(ct, _AAD, bad_key)
            pytest.fail("Expected InvalidEnvelopeError was not raised")
        except InvalidEnvelopeError as exc:
            assert bad_key.hex() not in str(exc)

    @pytest.mark.unit
    def test_invalid_envelope_error_message_does_not_expose_ciphertext_hex(self) -> None:
        ct = encrypt(_PLAINTEXT, _AAD, _KEY)
        tampered = bytes([99]) + ct[1:]
        try:
            decrypt(tampered, _AAD, _KEY)
            pytest.fail("Expected InvalidEnvelopeError was not raised")
        except InvalidEnvelopeError as exc:
            assert tampered.hex() not in str(exc)


class TestNonDeterminism:
    """Each encryption call uses a fresh nonce — identical inputs produce distinct outputs."""

    @pytest.mark.unit
    def test_two_encryptions_of_same_plaintext_differ(self) -> None:
        ct1 = encrypt(_PLAINTEXT, _AAD, _KEY)
        ct2 = encrypt(_PLAINTEXT, _AAD, _KEY)
        assert ct1 != ct2

    @pytest.mark.unit
    def test_different_nonces_both_decrypt_correctly(self) -> None:
        ct1 = encrypt(_PLAINTEXT, _AAD, _KEY)
        ct2 = encrypt(_PLAINTEXT, _AAD, _KEY)
        assert decrypt(ct1, _AAD, _KEY) == _PLAINTEXT
        assert decrypt(ct2, _AAD, _KEY) == _PLAINTEXT
