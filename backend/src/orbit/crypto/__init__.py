"""토큰 봉투 암호화 (ADR-0016 §3, D4-5). 구현은 `orbit.crypto.envelope`."""

from orbit.crypto.envelope import (
    EnvelopeCipher,
    EnvelopeError,
    KeyEncryptionKey,
    SealedSecret,
    load_kek_from_file,
)

__all__ = [
    "EnvelopeCipher",
    "EnvelopeError",
    "KeyEncryptionKey",
    "SealedSecret",
    "load_kek_from_file",
]
