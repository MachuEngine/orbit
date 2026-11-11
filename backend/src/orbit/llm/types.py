"""LLM 요청·응답 값 타입 (ADR-0009 §1).

모델은 제공자 ID가 아니라 작업 집합 핸들만 다룬다(D8-1). 이 모듈은 전송 형식을 모른다.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

Purpose = Literal["system_under_test", "data_generation", "judge"]
Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "other"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str | None = None
    tool_call_id: str | None = None  # role == "tool"일 때 응답 대상 호출 ID
    tool_calls: tuple[ToolCall, ...] = ()  # role == "assistant"일 때 모델이 낸 호출


@dataclass(frozen=True)
class ToolSpec:
    """모델에 노출하는 도구 정의. `parameters`는 JSON Schema 객체다."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ForcedToolChoice:
    """특정 도구 호출을 강제한다."""

    name: str


ToolChoice = Literal["auto", "none", "required"] | ForcedToolChoice


@dataclass(frozen=True)
class LLMRequest:
    messages: tuple[Message, ...]
    purpose: Purpose
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoice | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    metadata: dict[str, str] = field(default_factory=dict)  # 원문 금지. 과제 ID 같은 식별자만.


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass(frozen=True)
class LLMResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: FinishReason
    usage: LLMUsage
    cost_usd: float | None  # 알 수 없으면 None. 0.0과 구분한다.
    latency_ms: int
    model_id: str  # 실제 응답한 모델
    provider: str | None  # 실제 응답한 제공자. 응답에 없으면 None
    retries: int = 0  # 이 호출에서 일어난 재시도 횟수(사용량·지연은 재시도를 합산한 값)
