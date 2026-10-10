"""Unit tests for wallet-label encryption helpers (AUD-488).

Covers:
  - Round trip: encrypt_label -> decrypt_label recovers the original string.
  - Record-bound AAD: a label envelope cannot be decrypted under a different
    wallet id (the "must not be transplantable" requirement).
  - Wrong-key / missing-key behaviour: decrypt_label degrades to a placeholder
    instead of raising (see docs/operations.md#key-loss-behavior).
"""

import os
import uuid

import pytest

from audr.wallets.service import LABEL_UNREADABLE_PLACEHOLDER, decrypt_label, encrypt_label

_KEY = os.urandom(32)
_OTHER_KEY = os.urandom(32)


@pytest.mark.unit
def test_round_trip_recovers_original_label() -> None:
    wallet_id = uuid.uuid4()
    ciphertext = encrypt_label("Cold storage — café ☕", wallet_id, _KEY)
    assert decrypt_label(ciphertext, wallet_id, _KEY) == "Cold storage — café ☕"


@pytest.mark.unit
def test_round_trip_empty_label() -> None:
    wallet_id = uuid.uuid4()
    ciphertext = encrypt_label("", wallet_id, _KEY)
    assert decrypt_label(ciphertext, wallet_id, _KEY) == ""


@pytest.mark.unit
def test_two_encryptions_of_same_label_are_distinct_ciphertext() -> None:
    """Fresh nonce each call — confirms this reuses crypto.py's envelope, not a
    deterministic second path."""
    wallet_id = uuid.uuid4()
    ct1 = encrypt_label("same label", wallet_id, _KEY)
    ct2 = encrypt_label("same label", wallet_id, _KEY)
    assert ct1 != ct2


@pytest.mark.unit
def test_envelope_is_not_transplantable_to_another_wallet() -> None:
    """A label encrypted for one wallet id must not decrypt under another's —
    the AAD record-binding this issue requires (criterion 2)."""
    wallet_a = uuid.uuid4()
    wallet_b = uuid.uuid4()
    ciphertext = encrypt_label("Alice's wallet", wallet_a, _KEY)

    assert decrypt_label(ciphertext, wallet_b, _KEY) == LABEL_UNREADABLE_PLACEHOLDER
    # The correct wallet id still works — the envelope itself is intact.
    assert decrypt_label(ciphertext, wallet_a, _KEY) == "Alice's wallet"


@pytest.mark.unit
def test_wrong_key_degrades_to_placeholder_not_raise() -> None:
    """A rotated/wrong key must not 500 the caller — degrade to a placeholder
    (criterion 5; credentials raise here, labels do not)."""
    wallet_id = uuid.uuid4()
    ciphertext = encrypt_label("secret nickname", wallet_id, _KEY)

    assert decrypt_label(ciphertext, wallet_id, _OTHER_KEY) == LABEL_UNREADABLE_PLACEHOLDER


@pytest.mark.unit
def test_missing_key_degrades_to_placeholder_not_raise() -> None:
    wallet_id = uuid.uuid4()
    ciphertext = encrypt_label("secret nickname", wallet_id, _KEY)

    assert decrypt_label(ciphertext, wallet_id, None) == LABEL_UNREADABLE_PLACEHOLDER


@pytest.mark.unit
def test_corrupted_ciphertext_degrades_to_placeholder_not_raise() -> None:
    """Garbage bytes (e.g. a raw-SQL test fixture row, or bit rot) must not
    raise — only a well-formed, correctly-keyed envelope ever decrypts."""
    wallet_id = uuid.uuid4()

    assert decrypt_label(b"not an envelope", wallet_id, _KEY) == LABEL_UNREADABLE_PLACEHOLDER
    assert decrypt_label(b"", wallet_id, _KEY) == LABEL_UNREADABLE_PLACEHOLDER
