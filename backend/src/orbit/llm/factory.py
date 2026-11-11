"""설정으로 LLM 구현체를 고른다 (ADR-0014). 호출 코드는 제공자를 모른다."""

from typing import Any

import httpx

from orbit.core.config import LLMSettings
from orbit.llm.interface import LLM
from orbit.llm.openai import OpenAILLM
from orbit.llm.openrouter import OpenRouterLLM


def build_llm(settings: LLMSettings, transport: httpx.AsyncBaseTransport | None = None) -> LLM:
    if settings.llm_provider == "openai":
        return OpenAILLM(settings, transport)
    return OpenRouterLLM(settings, transport)


def llm_config_info(settings: LLMSettings) -> dict[str, Any]:
    """러너 결과 `config`에 남길 설정 지문. 키·URL은 넣지 않는다."""
    if settings.llm_provider == "openai":
        return {
            "provider": "openai",
            "reasoning_effort": settings.llm_reasoning_effort,
            "temperature_sent": settings.llm_send_temperature,
            "price_source": settings.llm_price_source,
        }
    return {
        "provider": "openrouter",
        "provider_order": list(settings.llm_provider_order),
        "reasoning_effort": None,
        "temperature_sent": True,
        "price_source": None,
    }
