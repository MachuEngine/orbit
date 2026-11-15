"""음성 입력 API (ADR-0025 §2·§3). `workspace_session` 안에서 동작하고 CSRF를 검사한다.

- `POST /api/voice/transcribe`: multipart 오디오(`audio` 파트 하나)를 받아 `{"text": ...}`를
  돌려준다.
- 오류는 `detail`에 종류만 담는다: `too_large`(413), `too_long`(413), `unsupported_format`(400),
  `bad_request`(400), `usage_limit_exceeded`(429), `provider_error`(502), `stt_unavailable`(503).
- 오디오는 메모리에서만 다루고 로그에는 크기·초·오류 종류만 남긴다. 전사 문장은 남기지 않는다.
"""

import logging
import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from orbit.api.auth import current_session
from orbit.auth import SessionInfo
from orbit.core.clock import Clock
from orbit.core.config import Settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.llm.stt import SpeechToText, SttError
from orbit.voice import usage
from orbit.voice.limits import (
    ASSUMED_BYTES_PER_SECOND,
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SECONDS,
    MULTIPART_OVERHEAD_BYTES,
)
from orbit.voice.upload import Audio, AudioUploadParser, UploadError

logger = logging.getLogger(__name__)

_BODY_LIMIT = MAX_AUDIO_BYTES + MULTIPART_OVERHEAD_BYTES


def _fail(status_code: int, kind: str, size: int | None = None) -> HTTPException:
    logger.warning("voice transcribe 실패 kind=%s bytes=%s", kind, size)
    return HTTPException(status_code, kind)


async def _read_upload(request: Request) -> Audio:
    """본문 상한을 넘으면 파싱을 멈추고 413이다. 오디오는 메모리에서만 다룬다."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _BODY_LIMIT:
        raise _fail(413, "too_large", int(declared))
    total = 0
    try:
        parser = AudioUploadParser(request.headers.get("content-type", ""))
        async for chunk in request.stream():
            total += len(chunk)
            if total > _BODY_LIMIT:
                raise _fail(413, "too_large", total)
            parser.feed(chunk)
        audio = parser.finish()
    except UploadError as exc:
        raise _fail(exc.status_code, exc.kind, total) from None
    return audio


def _charged_seconds(known: float | None, size: int) -> float:
    """사용량에 기록할 초. 길이를 알면 그 값, 모르면 크기로 보수적으로 추정한다."""
    if known is not None:
        return max(known, 0.0)
    return min(MAX_AUDIO_SECONDS, max(1.0, size / ASSUMED_BYTES_PER_SECOND))


def build_voice_router(stt: SpeechToText | None) -> APIRouter:
    router = APIRouter()
    Session = Annotated[SessionInfo, Depends(current_session)]  # noqa: N806

    @router.post("/voice/transcribe")
    async def transcribe(request: Request, session: Session) -> dict[str, Any]:
        if stt is None:
            raise _fail(503, "stt_unavailable")
        settings: Settings = request.app.state.settings
        db: AsyncDatabase = request.app.state.db
        clock: Clock = request.app.state.clock
        ids: Uuid7Generator = request.app.state.ids

        audio = await _read_upload(request)
        size = len(audio.data)
        if audio.duration_seconds is not None and (
            not math.isfinite(audio.duration_seconds) or audio.duration_seconds > MAX_AUDIO_SECONDS
        ):
            raise _fail(413, "too_long", size)
        seconds = _charged_seconds(audio.duration_seconds, size)

        reservation = await usage.reserve(
            db,
            clock,
            ids,
            session.workspace_id,
            seconds=seconds,
            size_bytes=size,
            max_per_hour=settings.voice_max_transcribes_per_hour,
            max_seconds_per_day=settings.voice_max_seconds_per_day,
        )
        if reservation.verdict != "ok" or reservation.usage_id is None:
            raise _fail(429, "usage_limit_exceeded", size)

        try:
            transcript = await stt.transcribe(
                audio.data, content_type=audio.content_type, language="ko"
            )
        except SttError as exc:
            raise _fail(502, "provider_error", size) from exc
        if transcript.duration_seconds is not None:
            try:
                await usage.correct(
                    db,
                    session.workspace_id,
                    reservation.usage_id,
                    seconds=transcript.duration_seconds,
                )
                seconds = transcript.duration_seconds
            except Exception as exc:  # noqa: BLE001 - 보정 실패가 이미 끝난 전사를 막지 않는다
                logger.warning("voice usage 보정 실패 kind=%s", type(exc).__name__)
        logger.info("voice transcribe 성공 bytes=%d seconds=%.1f", size, seconds)
        return {"text": transcript.text}

    return router
