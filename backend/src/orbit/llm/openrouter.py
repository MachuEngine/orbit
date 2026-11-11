"""OpenRouter(OpenAI 호환 `/chat/completions`) 구현 (ADR-0009 §1, D9-4).

API 키는 `Authorization` 헤더에만 쓰고, 로그·예외 메시지에는 넣지 않는다.
예외 메시지에는 상태 코드와 오류 코드만 담는다(응답 본문에 프롬프트가 되돌아올 수 있어서).
"""

import json
import re
from collections.abc import Callable
from typing import Any

import httpx

from orbit.core.config import LLMSettings
from orbit.llm.interface import (
    LLMRequestError,
    LLMTimeoutError,
    MalformedResponseError,
    ModelMismatchError,
    RetryPolicy,
    TransientLLMError,
    run_with_retry,
)
from orbit.llm.model_id import validate_model_id
from orbit.llm.types import (
    FinishReason,
    ForcedToolChoice,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    Message,
    ToolCall,
)

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "length": "length",
    "content_filter": "content_filter",
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class OpenRouterLLM:
    def __init__(
        self,
        settings: LLMSettings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._model_id = validate_model_id(settings.llm_model_id)
        self._provider_order = tuple(settings.llm_provider_order)
        self._api_key = settings.openrouter_api_key
        self._url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
        self._timeout = settings.llm_timeout_seconds
        self._transport = transport
        self._retry = retry_policy or RetryPolicy(max_retries=settings.llm_max_retries)
        self._sleep = sleep

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        return await run_with_retry(lambda: self._once(request), self._retry, **kwargs)

    def build_body(self, request: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model_id,
            "messages": [_message_to_wire(m) for m in request.messages],
            "provider": {
                "order": list(self._provider_order),
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
            },
            "usage": {"include": True},
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        if request.tool_choice is not None:
            choice = request.tool_choice
            body["tool_choice"] = (
                {"type": "function", "function": {"name": choice.name}}
                if isinstance(choice, ForcedToolChoice)
                else choice
            )
        if request.max_output_tokens is not None:
            body["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        return body

    async def _once(self, request: LLMRequest) -> LLMResponse:
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                resp = await c.post(self._url, json=self.build_body(request), headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"OpenRouter 요청 시간 초과 ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            raise TransientLLMError(f"OpenRouter 연결 오류 ({type(exc).__name__})") from None

        status = resp.status_code
        if status == 429 or status >= 500:
            raise TransientLLMError(f"OpenRouter 일시 오류: HTTP {status}{_error_code(resp)}")
        if status >= 400:
            raise LLMRequestError(f"OpenRouter 요청 거절: HTTP {status}{_error_code(resp)}")
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> LLMResponse:
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise MalformedResponseError("OpenRouter 응답에 choices[0].message가 없다") from None

        model = data.get("model")
        if model != self._model_id:
            raise ModelMismatchError(
                f"응답 모델이 설정과 다르다: 설정={self._model_id!r}, 응답={model!r}"
            )
        provider = data.get("provider")
        if isinstance(provider, str):
            allowed = {_normalize(p) for p in self._provider_order}
            if _normalize(provider) not in allowed:
                raise ModelMismatchError(
                    f"응답 제공자가 설정과 다르다: 설정={list(self._provider_order)!r}, "
                    f"응답={provider!r}"
                )
        else:
            provider = None

        tool_calls = tuple(_parse_tool_call(tc) for tc in (message.get("tool_calls") or []))
        usage_raw = data.get("usage") or {}
        details = usage_raw.get("prompt_tokens_details") or {}
        cost = usage_raw.get("cost")
        return LLMResponse(
            text=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=_FINISH_REASONS.get(str(choice.get("finish_reason")), "other"),
            usage=LLMUsage(
                input_tokens=int(usage_raw.get("prompt_tokens") or 0),
                output_tokens=int(usage_raw.get("completion_tokens") or 0),
                cached_input_tokens=int(details.get("cached_tokens") or 0),
            ),
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
            latency_ms=0,  # run_with_retry가 시도 전체 시간으로 채운다
            model_id=model,
            provider=provider,
        )


def _message_to_wire(m: Message) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_call_id is not None:
        wire["tool_call_id"] = m.tool_call_id
    if m.tool_calls:
        wire["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in m.tool_calls
        ]
    return wire


def _parse_tool_call(raw: Any) -> ToolCall:
    try:
        fn = raw["function"]
        args = json.loads(fn["arguments"]) if fn.get("arguments") else {}
        if not isinstance(args, dict):
            raise TypeError
        return ToolCall(id=str(raw["id"]), name=str(fn["name"]), arguments=args)
    except (KeyError, TypeError, ValueError):
        raise MalformedResponseError("도구 호출 형식이 올바르지 않다") from None


def _error_code(resp: httpx.Response) -> str:
    try:
        code = resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""
    return f" (code={code})" if isinstance(code, str | int) else ""
