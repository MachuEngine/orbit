import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from orbit.core.config import OpenAIAccess, Settings
from orbit.llm.stt import (
    FakeSpeechToText,
    InvalidSttModelIdError,
    OpenAISpeechToText,
    SttError,
    validate_stt_model_id,
)
from orbit.llm.stt_factory import build_stt
from orbit.runflow.fingerprint import config_fingerprint

KEY = "sk-test-SYNTHETIC-KEY-123"  # gitleaks:allow (합성 테스트 값)
AUDIO = b"\x1a\x45\xdf\xa3" + b"SYNTHETIC-AUDIO-MARKER"


def _stt(handler: Any, model: str = "gpt-transcribe") -> OpenAISpeechToText:
    return OpenAISpeechToText(
        api_key=SecretStr(KEY), model_id=model, transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize("bad", ["", "  ", "gpt-transcribe-latest", "latest", "default", "a b"])
def test_model_id_rejects_empty_and_aliases(bad: str) -> None:
    with pytest.raises(InvalidSttModelIdError):
        validate_stt_model_id(bad)
    with pytest.raises(InvalidSttModelIdError):
        OpenAISpeechToText(api_key=SecretStr(KEY), model_id=bad)


def test_model_id_accepts_pinned_ids() -> None:
    assert validate_stt_model_id("gpt-transcribe") == "gpt-transcribe"
    assert validate_stt_model_id("gpt-transcribe-2026-11-01") == "gpt-transcribe-2026-11-01"


@pytest.mark.parametrize("bad", ["", "latest", "x-latest"])
def test_settings_fail_at_startup_for_bad_stt_model(bad: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=SecretStr("postgresql://x"), stt_model_id=bad)


def test_settings_defaults() -> None:
    s = Settings(database_url=SecretStr("postgresql://x"))
    assert s.stt_model_id == "gpt-transcribe"
    assert s.stt_provider == "openai"
    assert s.voice_max_transcribes_per_hour == 60
    assert s.voice_max_seconds_per_day == 1800


def test_fingerprint_includes_stt_model() -> None:
    a = config_fingerprint("m", stt_model_id="gpt-transcribe")
    assert a == config_fingerprint("m")  # 기본 모델과 같다
    assert a != config_fingerprint("m", stt_model_id="gpt-transcribe-2026-11-01")


async def test_fake_is_deterministic() -> None:
    fake = FakeSpeechToText()
    a = await fake.transcribe(AUDIO, content_type="audio/webm")
    assert a == await fake.transcribe(AUDIO, content_type="audio/webm")
    assert a.text != (await fake.transcribe(AUDIO + b"x", content_type="audio/webm")).text


async def test_openai_request_shape_and_response() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["ctype"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " 안녕하세요 ", "usage": {"seconds": 3}})

    out = await _stt(handler).transcribe(AUDIO, content_type="audio/webm", language="ko")
    assert out.text == "안녕하세요"
    assert out.duration_seconds == 3.0
    assert seen["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["ctype"].startswith("multipart/form-data")
    body: bytes = seen["body"]
    assert b'name="model"' in body and b"gpt-transcribe" in body
    assert b'name="language"' in body and b"\r\n\r\nko\r\n" in body
    assert b'name="response_format"' in body
    assert b'filename="audio.webm"' in body and b"Content-Type: audio/webm" in body
    assert AUDIO in body
    assert KEY.encode() not in body  # 키는 헤더에만 있다


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, "transient"),
        (500, "transient"),
        (503, "transient"),
        (400, "request"),
        (401, "request"),
    ],
)
async def test_http_errors_map_to_kinds_without_content(status: int, kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "SYNTHETIC-BODY"}})

    with pytest.raises(SttError) as err:
        await _stt(handler).transcribe(AUDIO, content_type="audio/webm")
    assert err.value.kind == kind
    assert f"HTTP {status}" in str(err.value)
    assert "SYNTHETIC" not in str(err.value) and KEY not in str(err.value)


async def test_timeout_and_transport_errors() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no", request=request)

    with pytest.raises(SttError) as err:
        await _stt(timeout).transcribe(AUDIO, content_type="audio/webm")
    assert err.value.kind == "timeout"
    assert err.value.__context__ is None  # 요청 헤더(키)가 든 예외를 붙이지 않는다
    with pytest.raises(SttError) as err:
        await _stt(refused).transcribe(AUDIO, content_type="audio/webm")
    assert err.value.kind == "transient"
    assert err.value.__context__ is None


@pytest.mark.parametrize("body", [{"nope": 1}, {"text": 5}, [1, 2]])
async def test_malformed_response(body: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(body))

    with pytest.raises(SttError) as err:
        await _stt(handler).transcribe(AUDIO, content_type="audio/webm")
    assert err.value.kind == "malformed"


def test_build_stt_selects_by_settings() -> None:
    db = SecretStr("postgresql://x")
    assert isinstance(build_stt(Settings(database_url=db, stt_provider="fake")), FakeSpeechToText)
    assert build_stt(Settings(database_url=db), OpenAIAccess(openai_api_key=None)) is None
    built = build_stt(Settings(database_url=db), OpenAIAccess(openai_api_key=SecretStr(KEY)))
    assert isinstance(built, OpenAISpeechToText) and built.model_id == "gpt-transcribe"
