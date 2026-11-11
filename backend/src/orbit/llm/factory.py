"""설정으로 LLM 구현체를 고른다 (ADR-0014). 호출 코드는 제공자를 모른다."""

import httpx

from orbit.core.config import LLMSettings
from orbit.llm.config_info import llm_config_info
from orbit.llm.interface import LLM
from orbit.llm.openai import OpenAILLM
from orbit.llm.openrouter import OpenRouterLLM

__all__ = ["build_llm", "llm_config_info"]


def build_llm(settings: LLMSettings, transport: httpx.AsyncBaseTransport | None = None) -> LLM:
    if settings.llm_provider == "openai":
        return OpenAILLM(settings, transport)
    return OpenRouterLLM(settings, transport)
