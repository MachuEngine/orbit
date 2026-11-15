"""음성 전사(STT) 인터페이스와 구현 (ADR-0025 §2·§3).

- `SpeechToText.transcribe`는 오디오 바이트를 받아 `Transcript`를 돌려준다. 오디오는 메모리에만
  둔다.
- OpenAI 구현은 `POST /audio/transcriptions`를 부른다. 모델 ID는 설정에서만 받고 별칭·빈 값이면
  만들 때 실패한다. 키는 `Authorization` 헤더에만 쓰고, 예외 메시지에는 종류와 HTTP 상태만 담는다
  (오디오·전사 문장은 담지 않는다).
- 가짜 구현은 오디오 바이트의 해시로 문장을 만드는 결정적 구현이다. 테스트·Sim 전용이다.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import httpx
from pydantic import SecretStr

FAKE_STT_MODEL_ID = "fake-stt-v1"
DEFAULT_STT_MODEL_ID = "gpt-transcribe"  # ADR-0025 기본 후보(`실험 중`)

SttErrorKind = Literal["timeout", "transient", "request", "malformed"]

_ALIAS_SUFFIX = re.compile(r"(^|[-_.:])latest$", re.IGNORECASE)
_ALIAS_NAMES = frozenset({"default", "auto"})
_FILE_EXT = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/mp4": "m4a",
}


class InvalidSttModelIdError(ValueError):
    """STT 모델 ID가 비었거나 별칭이다."""


def validate_stt_model_id(model_id: str) -> str:
    """빈 값, 공백, `latest`류 별칭을 거절한다(D9-3). 통과하면 입력을 그대로 돌려준다."""
    if not model_id or not model_id.strip():
        raise InvalidSttModelIdError("STT 모델 ID가 비어 있다")
    if model_id != model_id.strip() or any(ch.isspace() for ch in model_id):
        raise InvalidSttModelIdError("STT 모델 ID에 공백이 있다")
    if _ALIAS_SUFFIX.search(model_id) or model_id.lower() in _ALIAS_NAMES:
        raise InvalidSttModelIdError("별칭은 고정 모델 ID가 아니다")
    return model_id


class SttError(Exception):
    """전사 실패. `kind`만 안전하게 노출한다."""

    def __init__(self, kind: SttErrorKind, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind: SttErrorKind = kind


@dataclass(frozen=True)
class Transcript:
    text: str
    duration_seconds: float | None = None


@runtime_checkable
class SpeechToText(Protocol):
    @property
    def model_id(self) -> str: ...

    async def transcribe(
        self, audio: bytes, *, content_type: str, language: str = "ko"
    ) -> Transcript: ...


class FakeSpeechToText:
    """결정적 가짜 전사. 같은 오디오는 같은 문장이고 실제로 듣지 않는다."""

    def __init__(self, model_id: str = FAKE_STT_MODEL_ID) -> None:
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    async def transcribe(
        self, audio: bytes, *, content_type: str, language: str = "ko"
    ) -> Transcript:
        digest = hashlib.sha256(audio).hexdigest()[:8]
        return Transcript(text=f"합성 전사 {digest}", duration_seconds=None)


class OpenAISpeechToText:
    def __init__(
        self,
        *,
        api_key: SecretStr,
        model_id: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("OpenAI API 키가 비어 있다")
        if not base_url.startswith("https://"):
            raise ValueError("base URL은 https://만 허용한다")
        self._api_key = api_key
        self._model_id = validate_stt_model_id(model_id)
        self._url = base_url.rstrip("/") + "/audio/transcriptions"
        self._timeout = timeout_s
        self._transport = transport

    @property
    def model_id(self) -> str:
        return self._model_id

    async def transcribe(
        self, audio: bytes, *, content_type: str, language: str = "ko"
    ) -> Transcript:
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        ext = _FILE_EXT.get(content_type, "bin")
        files = {"file": (f"audio.{ext}", audio, content_type)}
        data = {"model": self._model_id, "language": language, "response_format": "json"}
        # httpx 예외(요청 헤더에 키가 있다)가 __context__에 붙지 않게 except 밖에서 던진다.
        error: SttError | None = None
        resp: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                resp = await c.post(self._url, data=data, files=files, headers=headers)
        except httpx.TimeoutException:
            error = SttError("timeout", "전사 요청 시간 초과")
        except httpx.TransportError as exc:
            error = SttError("transient", f"전사 연결 오류 ({type(exc).__name__})")
        if error is not None or resp is None:
            raise error or SttError("transient")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise SttError("transient", f"전사 호출 실패: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise SttError("request", f"전사 호출 실패: HTTP {resp.status_code}")
        return _parse(resp)


def _parse(resp: httpx.Response) -> Transcript:
    try:
        body = resp.json()
        text = body["text"]
        if not isinstance(text, str):
            raise TypeError
        usage = body.get("usage")
        seconds: float | None = None
        if isinstance(usage, dict) and isinstance(usage.get("seconds"), int | float):
            seconds = float(usage["seconds"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SttError("malformed", "전사 응답을 해석하지 못했다") from None
    return Transcript(text=text.strip(), duration_seconds=seconds)
