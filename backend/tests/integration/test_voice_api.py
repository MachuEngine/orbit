"""음성 전사 API (ADR-0025): 인증·CSRF, 크기·형식·길이 상한, 사용량 상한, 워크스페이스 격리, 로그.

오디오는 무음이거나 형식 머리말만 있는 합성 바이트다.
"""

import asyncio
import logging
import struct
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from audio_support import ogg, wav, webm
from integration.mailindex_support import NOW, IndexEnv
from integration.runflow_support import StubResolver
from integration.support import WorkspaceFactory
from orbit.api.app import create_app
from orbit.auth import issue_session
from orbit.core.config import Settings
from orbit.llm.stt import FakeSpeechToText, SttError, Transcript
from orbit.voice.limits import MAX_AUDIO_BYTES, MAX_COMPRESSED_AUDIO_BYTES, USAGE_RETENTION

SECRET_SENTENCE = "합성비밀문장-하나둘셋"


class RecordingStt:
    """호출을 세고 고정 문장을 돌려준다. `fail`이 있으면 그 오류를 낸다."""

    model_id = "recording-stt"

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.fail: SttError | None = None
        self.seconds: float | None = None

    async def transcribe(
        self, audio: bytes, *, content_type: str, language: str = "ko"
    ) -> Transcript:
        self.calls.append((len(audio), content_type, language))
        if self.fail is not None:
            raise self.fail
        return Transcript(text=SECRET_SENTENCE, duration_seconds=self.seconds)


@dataclass
class VoiceApi:
    ix: IndexEnv
    client: httpx.AsyncClient
    transport: httpx.ASGITransport
    stt: RecordingStt
    extra: list[httpx.AsyncClient] = field(default_factory=list)

    def headers(self, client: httpx.AsyncClient | None = None) -> dict[str, str]:
        return {"X-CSRF-Token": (client or self.client).cookies.get("orbit_csrf") or ""}

    async def other_client(self, workspace: uuid.UUID) -> httpx.AsyncClient:
        issued = await issue_session(
            self.ix.db,
            workspace_id=workspace,
            clock=self.ix.clock,
            ids=self.ix.ids,
            ttl_seconds=3600,
        )
        client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        client.cookies.set("orbit_session", issued.token)
        client.cookies.set("orbit_csrf", issued.csrf_token)
        self.extra.append(client)
        return client

    async def post(
        self,
        data: bytes,
        content_type: str = "audio/webm",
        *,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        c = client or self.client
        return await c.post(
            "/api/voice/transcribe",
            files={"audio": ("rec.bin", data, content_type)},
            headers=self.headers(c) if headers is None else headers,
        )

    async def usage_rows(self, ws: uuid.UUID | None = None) -> list[dict[str, Any]]:
        async with self.ix.db.workspace_session(ws or self.ix.ws) as s:
            found = await s.execute(text("SELECT * FROM voice_usage ORDER BY at, id"))
            return [dict(r._mapping) for r in found]


MakeApi = Callable[..., Any]


@pytest.fixture
async def make_api(app_url: str, ix: IndexEnv) -> AsyncIterator[MakeApi]:
    stack: list[Any] = []

    async def make(stt: Any = "default", **settings_kw: Any) -> VoiceApi:
        settings = Settings(
            database_url=SecretStr(app_url),
            env="dev",
            dev_workspace_id=ix.ws,
            stt_provider="fake",
            session_ttl_seconds=10 * 86400.0,  # 시계를 며칠 당겨도 세션이 살아 있게 한다
            **settings_kw,
        )
        recording = RecordingStt()
        app = create_app(
            settings,
            clock=ix.clock,
            ids=ix.ids,
            resolver=StubResolver(ix.conns.calendar["acct-alice"], ix.mail),
            stt=recording if stt == "default" else stt,
        )
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        assert (await client.post("/api/dev/login")).status_code == 200
        made = VoiceApi(ix, client, transport, recording)
        stack.append((cm, client, made))
        return made

    yield make
    for cm, client, made in reversed(stack):
        for extra in made.extra:
            await extra.aclose()
        await client.aclose()
        await cm.__aexit__(None, None, None)


async def test_requires_session_and_csrf(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    anon = httpx.AsyncClient(transport=api.transport, base_url="http://test")
    try:
        assert (await api.post(webm(), client=anon, headers={})).status_code == 401
    finally:
        await anon.aclose()
    assert (await api.post(webm(), headers={})).status_code == 403  # 헤더 없음
    bad = await api.post(webm(), headers={"X-CSRF-Token": "wrong"})
    assert bad.status_code == 403 and bad.json()["detail"] == "csrf_mismatch"
    assert api.stt.calls == []
    assert await api.usage_rows() == []  # 거절된 요청은 사용량에 남지 않는다


async def test_success_returns_text_and_records_usage(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    data = webm(1000)
    response = await api.post(data, "audio/webm;codecs=opus")
    assert response.status_code == 200, response.text
    assert response.json() == {"text": SECRET_SENTENCE}
    assert api.stt.calls == [(len(data), "audio/webm", "ko")]
    [row] = await api.usage_rows()
    assert row["bytes"] == len(data) and row["at"] == NOW
    assert set(row) == {"id", "workspace_id", "at", "seconds", "bytes"}  # 내용 열이 없다


async def test_wav_duration_is_recorded_exactly(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    assert (await api.post(wav(4), "audio/wav")).status_code == 200
    [row] = await api.usage_rows()
    assert row["seconds"] == pytest.approx(4.0)


async def test_fake_stt_path_through_default_wiring(app_url: str, ix: IndexEnv) -> None:
    settings = Settings(
        database_url=SecretStr(app_url), env="dev", dev_workspace_id=ix.ws, stt_provider="fake"
    )
    app = create_app(settings, clock=ix.clock, ids=ix.ids)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        assert (await c.post("/api/dev/login")).status_code == 200
        h = {"X-CSRF-Token": c.cookies.get("orbit_csrf") or ""}
        files = {"audio": ("a.ogg", ogg(), "audio/ogg")}
        first = await c.post("/api/voice/transcribe", files=files, headers=h)
        second = await c.post("/api/voice/transcribe", files=files, headers=h)
    expected = (await FakeSpeechToText().transcribe(ogg(), content_type="audio/ogg")).text
    assert first.json() == second.json() == {"text": expected}


async def test_openai_without_key_is_503(
    app_url: str, ix: IndexEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORBIT_OPENAI_API_KEY", raising=False)
    settings = Settings(
        database_url=SecretStr(app_url), env="dev", dev_workspace_id=ix.ws, stt_provider="openai"
    )
    app = create_app(settings, clock=ix.clock, ids=ix.ids)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        assert (await c.post("/api/dev/login")).status_code == 200
        h = {"X-CSRF-Token": c.cookies.get("orbit_csrf") or ""}
        r = await c.post(
            "/api/voice/transcribe", files={"audio": ("a", webm(), "audio/webm")}, headers=h
        )
    assert r.status_code == 503 and r.json()["detail"] == "stt_unavailable"


async def test_size_limit_413(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    over = await api.post(webm(MAX_COMPRESSED_AUDIO_BYTES + 1), "audio/webm")  # 압축 형식 상한
    assert over.status_code == 413 and over.json()["detail"] == "too_large"
    huge = await api.post(webm(MAX_AUDIO_BYTES + 100_000), "audio/webm")  # 본문 상한을 넘는다
    assert huge.status_code == 413 and huge.json()["detail"] == "too_large"
    wav_over = await api.post(wav(60) + b"\x80" * MAX_AUDIO_BYTES, "audio/wav")
    assert wav_over.status_code == 413
    exact = await api.post(webm(MAX_COMPRESSED_AUDIO_BYTES), "audio/webm")
    assert exact.status_code == 200
    assert len(api.stt.calls) == 1
    assert len(await api.usage_rows()) == 1


async def test_content_length_over_limit_is_rejected_before_reading(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    r = await api.client.post(
        "/api/voice/transcribe",
        content=b"x",
        headers={**api.headers(), "content-length": str(MAX_AUDIO_BYTES + 1_000_000)},
    )
    assert r.status_code == 413 and r.json()["detail"] == "too_large"


async def test_forged_wav_header_is_judged_by_actual_bytes(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    forged = bytearray(wav(90))
    struct.pack_into("<I", forged, 40, 8000)  # 헤더는 1초라고 선언
    r = await api.post(bytes(forged), "audio/wav")
    assert r.status_code == 413 and r.json()["detail"] == "too_long"
    assert api.stt.calls == [] and await api.usage_rows() == []


async def test_provider_reported_seconds_correct_the_usage_row(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    api.stt.seconds = 42.5
    assert (await api.post(webm(8000))).status_code == 200  # 추정 2초
    [row] = await api.usage_rows()
    assert row["seconds"] == pytest.approx(42.5)


async def test_format_limits_400(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    for data, ctype in (
        (b"plain text", "text/plain"),
        (b"plain text", "audio/webm"),  # 주장과 내용이 다르다
        (webm(), "audio/mpeg"),
    ):
        r = await api.post(data, ctype)
        assert r.status_code == 400 and r.json()["detail"] == "unsupported_format"
    not_multipart = await api.client.post(
        "/api/voice/transcribe", content=b"{}", headers={**api.headers(), "content-type": "x/y"}
    )
    assert not_multipart.status_code == 400 and not_multipart.json()["detail"] == "bad_request"
    assert api.stt.calls == [] and await api.usage_rows() == []


async def test_duration_limit_413(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    ok = await api.post(wav(60), "audio/wav")
    assert ok.status_code == 200
    long = await api.post(wav(61), "audio/wav")
    assert long.status_code == 413 and long.json()["detail"] == "too_long"
    assert len(api.stt.calls) == 1


async def test_hourly_count_limit_429_and_window(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api(voice_max_transcribes_per_hour=3)
    for _ in range(3):
        assert (await api.post(webm())).status_code == 200
    blocked = await api.post(webm())
    assert blocked.status_code == 429 and blocked.json()["detail"] == "usage_limit_exceeded"
    assert len(api.stt.calls) == 3  # 제공자를 부르지 않았다
    api.ix.clock.advance(timedelta(minutes=61))
    assert (await api.post(webm())).status_code == 200


async def test_daily_seconds_limit_429_and_window(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api(
        voice_max_seconds_per_day=50.0, voice_max_transcribes_per_hour=99
    )
    assert (await api.post(wav(20), "audio/wav")).status_code == 200
    assert (await api.post(wav(20), "audio/wav")).status_code == 200
    blocked = await api.post(wav(20), "audio/wav")  # 40 + 20 > 50
    assert blocked.status_code == 429 and blocked.json()["detail"] == "usage_limit_exceeded"
    assert (await api.post(wav(10), "audio/wav")).status_code == 200  # 정확히 50은 통과
    api.ix.clock.advance(timedelta(hours=24, minutes=1))
    assert (await api.post(wav(20), "audio/wav")).status_code == 200


async def test_unknown_length_is_charged_conservatively(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    assert (await api.post(webm(8000))).status_code == 200  # 8000바이트 -> 2초 이상으로 본다
    assert (await api.post(webm(MAX_COMPRESSED_AUDIO_BYTES), "audio/webm")).status_code == 200
    rows = await api.usage_rows()
    assert rows[0]["seconds"] == pytest.approx(2.0, abs=0.1)
    assert rows[1]["seconds"] == 60.0  # 60초 상한으로 자른다


async def test_limits_are_per_workspace(make_api: MakeApi, workspaces: WorkspaceFactory) -> None:
    api: VoiceApi = await make_api(voice_max_transcribes_per_hour=1)
    other_ws = workspaces.create()
    other = await api.other_client(other_ws)
    assert (await api.post(webm())).status_code == 200
    assert (await api.post(webm())).status_code == 429
    assert (await api.post(webm(), client=other)).status_code == 200  # 다른 워크스페이스는 별개다
    assert (await api.post(webm(), client=other)).status_code == 429
    assert len(await api.usage_rows()) == 1
    assert len(await api.usage_rows(other_ws)) == 1


async def test_rls_hides_other_workspace_rows(
    make_api: MakeApi, workspaces: WorkspaceFactory
) -> None:
    api: VoiceApi = await make_api()
    other_ws = workspaces.create()
    other = await api.other_client(other_ws)
    assert (await api.post(webm(), client=other)).status_code == 200
    assert await api.usage_rows() == []  # 이 워크스페이스 세션에서는 다른 행이 보이지 않는다
    async with api.ix.db.workspace_session(api.ix.ws) as s:
        with pytest.raises(Exception, match="row-level security"):
            await s.execute(
                text(
                    "INSERT INTO voice_usage (id, workspace_id, at, seconds, bytes)"
                    " VALUES (:id, :ws, :at, 1, 1)"
                ),
                {"id": uuid.uuid4(), "ws": other_ws, "at": NOW},
            )


async def test_old_usage_rows_are_pruned_on_new_record(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    assert (await api.post(webm())).status_code == 200
    api.ix.clock.advance(USAGE_RETENTION + timedelta(minutes=1))
    assert (await api.post(webm())).status_code == 200
    rows = await api.usage_rows()
    assert len(rows) == 1 and rows[0]["at"] == api.ix.clock.now()


async def test_provider_error_maps_to_502_and_keeps_usage(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api()
    api.stt.fail = SttError("transient", "전사 호출 실패: HTTP 500")
    r = await api.post(webm())
    assert r.status_code == 502 and r.json()["detail"] == "provider_error"
    assert len(await api.usage_rows()) == 1


async def test_logs_hold_only_size_seconds_and_kind(
    make_api: MakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    api: VoiceApi = await make_api()
    marker = b"AUDIO-MARKER-" + b"Z" * 40
    caplog.set_level(logging.DEBUG)
    ok = await api.post(webm(0) + marker, "audio/webm")
    assert ok.status_code == 200
    api.stt.fail = SttError("request", "전사 호출 실패: HTTP 400")
    assert (await api.post(webm(0) + marker)).status_code == 502
    assert (await api.post(b"bad", "text/plain")).status_code == 400
    logged = caplog.text
    assert "voice transcribe" in logged  # 로그는 남기되
    assert SECRET_SENTENCE not in logged
    assert marker.decode() not in logged and "AUDIO-MARKER" not in logged
    assert "kind=provider_error" in logged and "kind=unsupported_format" in logged


async def test_concurrent_requests_cannot_exceed_hourly_cap(make_api: MakeApi) -> None:
    api: VoiceApi = await make_api(voice_max_transcribes_per_hour=3)
    results = await asyncio.gather(*(api.post(webm()) for _ in range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes == [200] * 3 + [429] * 5
    assert len(await api.usage_rows()) == 3
