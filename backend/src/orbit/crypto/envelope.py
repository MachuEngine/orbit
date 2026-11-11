"""토큰 봉투 암호화 (ADR-0016 §3, D4-5).

- 비밀(리프레시 토큰)마다 무작위 데이터 키(DEK, 32바이트)를 만들어 AES-256-GCM으로 암호화한다.
- DEK는 키 암호화 키(KEK)로 다시 AES-256-GCM 래핑해 암호문 옆에 저장한다. 평문 DEK는 저장 안 한다.
- 연관 데이터(AAD)는 `connection_id || workspace_id`(UUID 각 16바이트, 모두 32바이트)다. 본문과 DEK
  래핑에 같은 AAD를 쓰므로 다른 행으로 옮긴 암호문은 복호화되지 않는다.
- 암호문과 함께 `kek_id`를 기록한다. 복호화 때 지금 KEK의 ID와 다르면 예외다(회전 대비).
  KEK ID는 키에서 도메인 분리 해시로 만든 짧은 식별자라 키 값을 알려 주지 않는다.
- nonce(96비트)는 호출마다 무작위다. 같은 평문도 암호문이 매번 다르다.
- 오류에는 종류만 담는다. 비밀·키·암호문은 메시지와 `repr`에 넣지 않는다.
- KEK 파일: 32바이트 원시 값 또는 그 base64 문자열. 권한이 0600이 아니면 경고만 한다(경로만 기록).
"""

import base64
import binascii
import hashlib
import logging
import os
import secrets
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

logger = logging.getLogger(__name__)

KEY_BYTES = 32
NONCE_BYTES = 12
_KEK_ID_CONTEXT = b"orbit.kek.id.v1:"

EnvelopeErrorKind = Literal[
    "kek_unavailable",  # KEK 파일을 읽을 수 없거나 형식이 맞지 않음
    "kek_mismatch",  # 암호문의 kek_id가 지금 KEK와 다름
    "decrypt_failed",  # AAD 불일치, 변조, 형식 오류
]


class EnvelopeError(Exception):
    """봉투 암호화 오류. 메시지에는 종류(와 KEK 파일 경로)만 담는다."""

    def __init__(self, kind: EnvelopeErrorKind, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind: EnvelopeErrorKind = kind


@dataclass(frozen=True)
class SealedSecret:
    """DB에 저장하는 값. 네 필드가 `connection`의 열과 일대일로 대응한다."""

    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    wrapped_dek: bytes = field(repr=False)
    kek_id: str


@dataclass(frozen=True)
class KeyEncryptionKey:
    key: bytes = field(repr=False)
    kek_id: str = ""

    def __post_init__(self) -> None:
        if len(self.key) != KEY_BYTES:
            raise EnvelopeError("kek_unavailable", f"KEK는 {KEY_BYTES}바이트여야 한다")
        if not self.kek_id:
            digest = hashlib.sha256(_KEK_ID_CONTEXT + self.key).hexdigest()[:16]
            object.__setattr__(self, "kek_id", f"kek1-{digest}")


def _parse_key_material(raw: bytes) -> bytes | None:
    if len(raw) == KEY_BYTES:
        return raw
    text = raw.strip()
    padded = text + b"=" * (-len(text) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) == KEY_BYTES:
            return decoded
    return None


def load_kek_from_file(path: str | Path) -> KeyEncryptionKey:
    """KEK 파일을 읽는다. 읽을 수 없거나 형식이 틀리면 `EnvelopeError`(메시지에 경로만)."""
    target = Path(path)
    try:
        raw = target.read_bytes()
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        raise EnvelopeError(
            "kek_unavailable", f"ORBIT_DATA_KEY_FILE을 읽을 수 없다: {target}"
        ) from None
    key = _parse_key_material(raw)
    if key is None:
        raise EnvelopeError(
            "kek_unavailable",
            f"ORBIT_DATA_KEY_FILE 형식이 맞지 않는다(32바이트 또는 base64): {target}",
        )
    if mode & 0o077:
        logger.warning("KEK 파일 권한이 0600보다 넓다. chmod 600 필요: %s", target)
    return KeyEncryptionKey(key)


def write_new_kek_file(path: str | Path) -> None:
    """새 무작위 KEK를 base64로 파일에 쓴다. 파일은 0600으로 만들고 이미 있으면 거부한다."""
    target = Path(path)
    key = secrets.token_bytes(KEY_BYTES)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, base64.b64encode(key) + b"\n")
    finally:
        os.close(fd)
    target.chmod(0o600)  # umask와 무관하게 0600


def _aad(connection_id: uuid.UUID, workspace_id: uuid.UUID) -> bytes:
    return connection_id.bytes + workspace_id.bytes


class EnvelopeCipher:
    def __init__(self, kek: KeyEncryptionKey) -> None:
        self._kek = kek
        self._kek_aead = AESGCM(kek.key)

    @property
    def kek_id(self) -> str:
        return self._kek.kek_id

    def seal(
        self, plaintext: str, *, connection_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> SealedSecret:
        aad = _aad(connection_id, workspace_id)
        dek = secrets.token_bytes(KEY_BYTES)
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad)
        wrap_nonce = secrets.token_bytes(NONCE_BYTES)
        wrapped = wrap_nonce + self._kek_aead.encrypt(wrap_nonce, dek, aad)
        return SealedSecret(
            ciphertext=ciphertext, nonce=nonce, wrapped_dek=wrapped, kek_id=self._kek.kek_id
        )

    def open(
        self, sealed: SealedSecret, *, connection_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> SecretStr:
        """복호화한다. `SecretStr`로 돌려주어 로그·`repr`에 원문이 찍히지 않게 한다."""
        if sealed.kek_id != self._kek.kek_id:
            raise EnvelopeError("kek_mismatch")
        aad = _aad(connection_id, workspace_id)
        wrapped = sealed.wrapped_dek
        if len(wrapped) <= NONCE_BYTES or len(sealed.nonce) != NONCE_BYTES:
            raise EnvelopeError("decrypt_failed")
        try:
            dek = self._kek_aead.decrypt(wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], aad)
            plain = AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, aad)
            return SecretStr(plain.decode("utf-8"))
        except (InvalidTag, ValueError):
            raise EnvelopeError("decrypt_failed") from None
