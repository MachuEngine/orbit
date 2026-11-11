"""LLM 설정 지문. 러너 결과 `config.llm`과 Run 설정 지문의 단일 출처다.

키·URL은 넣지 않는다. 보내지 않는 파라미터(temperature)와 되돌려 보내는 항목도 드러낸다.
"""

from typing import Any

from orbit.core.config import LLMSettings


def llm_config_info(settings: LLMSettings) -> dict[str, Any]:
    accepted = sorted(settings.llm_accepted_response_model_ids)
    prices = {
        "price_input_per_mtok": settings.llm_price_input_per_mtok,
        "price_cached_input_per_mtok": settings.llm_price_cached_input_per_mtok,
        "price_output_per_mtok": settings.llm_price_output_per_mtok,
        "price_source": settings.llm_price_source,
    }
    if settings.llm_provider == "openai":
        effort = settings.llm_reasoning_effort
        return {
            "provider": "openai",
            "reasoning_effort": effort,
            "temperature_sent": settings.llm_send_temperature,
            "tool_strict": False,
            # effort가 none이 아니면 암호화된 추론 항목을 요청하고 다음 요청에 되돌린다.
            "reasoning_items_roundtrip": effort != "none",
            "accepted_response_models": accepted,
            **prices,
        }
    return {
        "provider": "openrouter",
        "provider_order": list(settings.llm_provider_order),
        "reasoning_effort": None,
        "temperature_sent": True,
        "tool_strict": None,
        "reasoning_items_roundtrip": False,
        "accepted_response_models": accepted,
        **prices,
    }
