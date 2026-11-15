"""설정으로 STT 구현체를 고른다 (ADR-0025). 호출 코드는 제공자를 모른다."""

import httpx

from orbit.core.config import OpenAIAccess, Settings
from orbit.llm.stt import FakeSpeechToText, OpenAISpeechToText, SpeechToText


def build_stt(
    settings: Settings,
    access: OpenAIAccess | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SpeechToText | None:
    """fake면 가짜 구현, openai면 OpenAI 구현이다. openai인데 키가 없으면 None(API는 503)이다.

    모델 ID가 비었거나 별칭이면 `Settings`를 만들 때 이미 실패한다. 키가 있으면 여기서도
    다시 검증한다.
    """
    if settings.stt_provider == "fake":
        return FakeSpeechToText()
    resolved = access if access is not None else OpenAIAccess()
    if resolved.openai_api_key is None:
        return None
    return OpenAISpeechToText(
        api_key=resolved.openai_api_key,
        model_id=settings.stt_model_id,
        base_url=resolved.openai_base_url,
        transport=transport,
    )
