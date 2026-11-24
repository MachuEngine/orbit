"""A3(Adaptive) 라우터(ADR-0036). 질문 문장만 보고 {none, A1, A2} 중 하나를 고른다.

gold·태그·질문 유형·과제 ID는 프롬프트에 넣지 않는다(`task_id`는 요청 메타데이터에만 있다).
진단에는 경로 라벨과 토큰·비용·지연 숫자만 남긴다(ADR-0006).
"""

import json
from typing import Any

from orbit.assistant.agent import TEMPERATURE
from orbit.evalrunner.rag_arch.config import (
    A3_FALLBACK_ROUTE,
    A3_ROUTE_MAX_OUTPUT_TOKENS,
    A3_ROUTE_PROMPT,
    A3_ROUTES,
)
from orbit.llm.interface import LLM
from orbit.llm.types import LLMRequest, Message

# ADR-0036 "A3 라우팅 기대표"(결과를 보기 전에 고정). "none"이 기대 경로인 유형은 없다.
EXPECTED_ROUTE = {
    "rag:single": "A1",
    "rag:multihop": "A2",
    "rag:aggregate": "A2",
    "rag:unanswerable": "A1",
}


def parse_route(text: str | None) -> str | None:
    """`{"route":"none"|"A1"|"A2"}`만 받는다. 그 밖은 None."""
    try:
        data = json.loads((text or "").strip())
    except ValueError:
        return None
    route = data.get("route") if isinstance(data, dict) else None
    return route if route in A3_ROUTES else None


async def route_question(llm: LLM, question: str, task_id: str) -> tuple[str, dict[str, Any]]:
    """반환은 (경로, 진단). 진단: `a3_route`, `route_fallback`, 라우터 토큰·비용·지연."""
    response = await llm.complete(
        LLMRequest(
            messages=(
                Message(role="system", content=A3_ROUTE_PROMPT),
                Message(role="user", content=question),
            ),
            purpose="system_under_test",
            max_output_tokens=A3_ROUTE_MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
            metadata={"task_id": task_id, "step_kind": "a3_route"},
        )
    )
    route = None
    if not response.tool_calls and response.finish_reason != "length":
        route = parse_route(response.text)
    return (route or A3_FALLBACK_ROUTE), {
        "a3_route": route or A3_FALLBACK_ROUTE,
        "route_fallback": route is None,
        "route_input_tokens": response.usage.input_tokens,
        "route_output_tokens": response.usage.output_tokens,
        "route_cost_usd": response.cost_usd,
        "route_latency_ms": response.latency_ms,
    }
