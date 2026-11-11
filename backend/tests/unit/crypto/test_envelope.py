"""봉투 암호화 (ADR-0016 §3). 키·토큰은 모두 합성 값이다."""

import base64
import logging
import os
import stat
import uuid
from pathlib import Path

import pytest

from orbit.crypto.envelope import (
    EnvelopeCipher,
    EnvelopeError,
    KeyEncryptionKey,
    SealedSecret,
    load_kek_from_file,
)
from orbit.crypto.keygen import main as keygen_main

SECRET = "test-refresh-token-SYNTH-xyz"
CONN = uuid.UUID("00000000-0000-7000-8000-000000000001")
WS = uuid.UUID("00000000-0000-7000-8000-000000000002")
OTHER = uuid.UUID("00000000-0000-7000-8000-000000000003")


def _cipher(seed: int = 1) -> EnvelopeCipher:
    return EnvelopeCipher(KeyEncryptionKey(bytes([seed]) * 32))


def test_round_trip() -> None:
    c = _cipher()
    sealed = c.seal(SECRET, connection_id=CONN, workspace_id=WS)
    assert c.open(sealed, connection_id=CONN, workspace_id=WS).get_secret_value() == SECRET
    assert SECRET.encode() not in sealed.ciphertext + sealed.wrapped_dek + sealed.nonce


def test_same_plaintext_gives_different_ciphertext() -> None:
    c = _cipher()
    a = c.seal(SECRET, connection_id=CONN, workspace_id=WS)
    b = c.seal(SECRET, connection_id=CONN, workspace_id=WS)
    assert a.ciphertext != b.ciphertext
    assert a.nonce != b.nonce
    assert a.wrapped_dek != b.wrapped_dek


@pytest.mark.parametrize("swap", ["connection", "workspace"])
def test_aad_swap_fails(swap: str) -> None:
    c = _cipher()
    sealed = c.seal(SECRET, connection_id=CONN, workspace_id=WS)
    with pytest.raises(EnvelopeError) as info:
        if swap == "connection":
            c.open(sealed, connection_id=OTHER, workspace_id=WS)
        else:
            c.open(sealed, connection_id=CONN, workspace_id=OTHER)
    assert info.value.kind == "decrypt_failed"


def test_kek_mismatch_fails() -> None:
    sealed = _cipher(1).seal(SECRET, connection_id=CONN, workspace_id=WS)
    with pytest.raises(EnvelopeError) as info:
        _cipher(2).open(sealed, connection_id=CONN, workspace_id=WS)
    assert info.value.kind == "kek_mismatch"


def test_tampered_ciphertext_and_wrong_kek_id_fail() -> None:
    c = _cipher()
    sealed = c.seal(SECRET, connection_id=CONN, workspace_id=WS)
    flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    bad = SealedSecret(flipped, sealed.nonce, sealed.wrapped_dek, sealed.kek_id)
    with pytest.raises(EnvelopeError):
        c.open(bad, connection_id=CONN, workspace_id=WS)
    other_id = SealedSecret(sealed.ciphertext, sealed.nonce, sealed.wrapped_dek, "kek1-other")
    with pytest.raises(EnvelopeError) as info:
        c.open(other_id, connection_id=CONN, workspace_id=WS)
    assert info.value.kind == "kek_mismatch"


def test_repr_and_errors_hide_secrets() -> None:
    c = _cipher()
    sealed = c.seal(SECRET, connection_id=CONN, workspace_id=WS)
    opened = c.open(sealed, connection_id=CONN, workspace_id=WS)
    assert SECRET not in repr(opened)
    assert SECRET not in repr(sealed)
    assert "ciphertext=" not in repr(sealed)
    assert ("\x01" * 32) not in repr(KeyEncryptionKey(bytes([1]) * 32))


def test_load_raw_and_base64_key_files(tmp_path: Path) -> None:
    raw = tmp_path / "raw.key"
    raw.write_bytes(bytes(range(32)))
    raw.chmod(0o600)
    encoded = tmp_path / "b64.key"
    encoded.write_bytes(base64.b64encode(bytes(range(32))) + b"\n")
    encoded.chmod(0o600)
    assert load_kek_from_file(raw).kek_id == load_kek_from_file(encoded).kek_id


def test_missing_key_file_error_names_only_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.key"
    with pytest.raises(EnvelopeError) as info:
        load_kek_from_file(missing)
    assert str(missing) in str(info.value)
    assert info.value.kind == "kek_unavailable"


def test_bad_key_file_content_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.key"
    bad.write_text("not a key", encoding="utf-8")
    with pytest.raises(EnvelopeError) as info:
        load_kek_from_file(bad)
    assert "not a key" not in str(info.value)


def test_loose_permissions_only_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "loose.key"
    path.write_bytes(bytes(32))
    path.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        load_kek_from_file(path)
    assert any(str(path) in r.getMessage() for r in caplog.records)


def test_keygen_creates_0600_file_and_refuses_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "dev.key"
    old = os.umask(0o022)
    try:
        assert keygen_main(["keygen", str(path)]) == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    key = load_kek_from_file(path)
    assert len(key.key) == 32
    assert base64.b64encode(key.key).decode() not in capsys.readouterr().out
    assert keygen_main(["keygen", str(path)]) == 1
