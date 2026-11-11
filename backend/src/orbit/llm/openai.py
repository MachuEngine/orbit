"""OpenAI Responses API(`POST /responses`) 구현 (ADR-0014, 파일럿용 임시).

- API 키는 `Authorization` 헤더에만 쓴다. 예외 메시지에는 상태 코드와 오류 코드만 담는다.
- `store=false`로 요청한다(OpenAI 쪽에 응답을 저장하지 않는다).
- 모델 ID는 `openai/<모델>` 형식으로 설정하고, 호출할 때는 접두를 뗀다. OpenAI는 스냅샷 ID를
  공개하지 않으므로 응답의 실제 모델 값을 `openai/<응답 모델>`로 기록한다.
- 추론 토큰은 `output_tokens`에 이미 포함돼 있다고 가정하고(문서 기준) 출력 단가로 계산한다.
  따로 더하지 않는다. `LLMUsage.reasoning_tokens`는 그 부분집합을 기록만 한다.
- 추론 항목(reasoning item)은 다음 요청에 되돌려 보내지 않는다. `LLM` 인터페이스에 담을 곳이 없다.
"""

import json
import math
import re
from collections.abc import Callable
from typing import Any

import httpx

from orbit.core.config import LLMSettings
from orbit.llm.interface import (
    LLMError,
    LLMRequestError,
    LLMTimeoutError,
    MalformedResponseError,
    ModelMismatchError,
    RetryPolicy,
    TransientLLMError,
    run_with_retry,
)
from orbit.llm.model_id import InvalidModelIdError, validate_model_id
from orbit.llm.types import (
    FinishReason,
    ForcedToolChoice,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    Message,
    ToolCall,
)

PROVIDER_NAME = "openai"
_PREFIX = "openai/"
_MAX_MODEL_ID_LEN = 200
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


class OpenAILLM:
    def __init__(
        self,
        settings: LLMSettings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        full_id = validate_model_id(settings.llm_model_id)
        if not full_id.startswith(_PREFIX):
            raise InvalidModelIdError("openai provider의 모델 ID는 `openai/<모델>` 형식이어야 한다")
        if settings.openai_api_key is None:
            raise ValueError("OpenAI API 키가 없다")
        self._declared_id = full_id
        self._model = full_id[len(_PREFIX) :]
        self._accepted_ids = frozenset(settings.llm_accepted_response_model_ids)
        self._api_key = settings.openai_api_key
        self._url = settings.openai_base_url.rstrip("/") + "/responses"
        self._timeout = settings.llm_timeout_seconds
        self._effort = settings.llm_reasoning_effort
        self._send_temperature = settings.llm_send_temperature
        self._price_in = settings.llm_price_input_per_mtok
        self._price_cached = settings.llm_price_cached_input_per_mtok
        self._price_out = settings.llm_price_output_per_mtok
        self._price_source = settings.llm_price_source
        self._transport = transport
        self._retry = retry_policy or RetryPolicy(max_retries=settings.llm_max_retries)
        self._sleep = sleep

    @property
    def fingerprint_info(self) -> dict[str, Any]:
        """설정 지문에 넣을 값. 보내지 않는 파라미터도 여기에 드러낸다."""
        return {
            "provider": PROVIDER_NAME,
            "reasoning_effort": self._effort,
            "temperature_sent": self._send_temperature,
            "price_source": self._price_source,
        }

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        return await run_with_retry(lambda: self._once(request), self._retry, **kwargs)

    def build_body(self, request: LLMRequest) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for m in request.messages:
            items.extend(_message_to_items(m))
        body: dict[str, Any] = {"model": self._model, "input": items, "store": False}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in request.tools
            ]
        if request.tool_choice is not None:
            choice = request.tool_choice
            body["tool_choice"] = (
                {"type": "function", "name": choice.name}
                if isinstance(choice, ForcedToolChoice)
                else choice
            )
        if request.max_output_tokens is not None:
            body["max_output_tokens"] = request.max_output_tokens
        if self._effort is not None:
            body["reasoning"] = {"effort": self._effort}
        if self._send_temperature and request.temperature is not None:
            body["temperature"] = request.temperature
        return body

    async def _once(self, request: LLMRequest) -> LLMResponse:
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        # 예외는 except 블록 밖에서 던진다. httpx 예외(요청 헤더에 키가 있다)가 __context__에
        # 붙지 않게 하려는 것이다.
        error: LLMError | None = None
        resp: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                resp = await c.post(self._url, json=self.build_body(request), headers=headers)
        except httpx.TimeoutException as exc:
            error = LLMTimeoutError(f"OpenAI 요청 시간 초과 ({type(exc).__name__})")
        except httpx.TransportError as exc:
            error = TransientLLMError(f"OpenAI 연결 오류 ({type(exc).__name__})")
        if error is not None or resp is None:
            raise error or TransientLLMError("OpenAI 응답 없음")

        status = resp.status_code
        if status == 429 or status >= 500:
            raise TransientLLMError(f"OpenAI 일시 오류: HTTP {status}{_error_code(resp)}")
        if status >= 400:
            raise LLMRequestError(f"OpenAI 요청 거절: HTTP {status}{_error_code(resp)}")

        parsed: LLMResponse | None = None
        try:
            parsed = self._parse(resp)
        except LLMError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - 어떤 파싱 실패든 원문 없이 한 오류로 바꾼다
            error = MalformedResponseError(f"OpenAI 응답을 해석하지 못했다 ({type(exc).__name__})")
        if error is not None:
            error.__context__ = None
            error.__cause__ = None
            raise error
        assert parsed is not None  # noqa: S101
        return parsed

    def _model_accepted(self, model: str) -> bool:
        if model == self._model or model in self._accepted_ids:
            return True
        if _PREFIX + model in self._accepted_ids:
            return True
        # OpenAI가 응답에 날짜 접미를 붙이는 경우만 같은 모델로 본다.
        return (
            re.fullmatch(re.escape(self._model) + r"-(\d{4}-\d{2}-\d{2}|\d{8})", model) is not None
        )

    def _cost(self, usage: LLMUsage) -> float | None:
        if self._price_in is None or self._price_cached is None or self._price_out is None:
            return None
        cached = min(usage.cached_input_tokens, usage.input_tokens)
        fresh = usage.input_tokens - cached
        # output_tokens에 추론 토큰이 포함돼 있으므로 출력 단가로 한 번만 계산한다.
        total = fresh * self._price_in + cached * self._price_cached
        total += usage.output_tokens * self._price_out
        return total / 1_000_000

    def _parse(self, resp: httpx.Response) -> LLMResponse:
        try:
            data = resp.json()
            output = data["output"]
            if not isinstance(output, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise MalformedResponseError("OpenAI 응답에 output 목록이 없다") from None

        model = data.get("model")
        if model is not None and not isinstance(model, str):
            raise MalformedResponseError("응답 model 필드가 문자열이 아니다")

        usage_raw = data.get("usage") or {}
        in_details = usage_raw.get("input_tokens_details") or {}
        out_details = usage_raw.get("output_tokens_details") or {}
        usage = LLMUsage(
            input_tokens=_count(usage_raw.get("input_tokens")),
            output_tokens=_count(usage_raw.get("output_tokens")),
            cached_input_tokens=_count(in_details.get("cached_tokens")),
            reasoning_tokens=_count(out_details.get("reasoning_tokens")),
        )
        cost_usd = self._cost(usage)

        recorded_model: str | None = None
        too_long = model is not None and len(model) > _MAX_MODEL_ID_LEN
        if model is not None:
            recorded_model = _PREFIX + (model[:_MAX_MODEL_ID_LEN] if too_long else model)
        if model is not None and (too_long or not self._model_accepted(model)):
            mismatch = ModelMismatchError(
                f"응답 모델이 설정과 다르다: 설정={self._declared_id!r}, 응답={recorded_model!r}"
            )
            mismatch.usage = usage
            mismatch.cost_usd = cost_usd
            mismatch.response_model_id = recorded_model
            mismatch.response_provider = PROVIDER_NAME
            raise mismatch

        if data.get("status") == "failed":
            code = _body_error_code(data)
            failure: LLMError = (
                TransientLLMError(f"OpenAI 응답 실패{code}")
                if code == " (code=server_error)"
                else LLMRequestError(f"OpenAI 응답 실패{code}")
            )
            failure.usage = usage
            failure.cost_usd = cost_usd
            failure.response_model_id = recorded_model
            failure.response_provider = PROVIDER_NAME
            raise failure

        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        refused = False
        for item in output:
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        texts.append(part["text"])
                    elif part.get("type") == "refusal":
                        refused = True
            elif kind == "function_call":
                tool_calls.append(_parse_tool_call(item))

        return LLMResponse(
            text="".join(texts) if texts else None,
            tool_calls=tuple(tool_calls),
            finish_reason=_finish_reason(data, bool(tool_calls), refused),
            usage=usage,
            cost_usd=cost_usd,
            latency_ms=0,  # run_with_retry가 시도 전체 시간으로 채운다
            model_id=recorded_model,
            provider=PROVIDER_NAME,
        )


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(int(value), 0)


def _finish_reason(data: dict[str, Any], has_tool_calls: bool, refused: bool) -> FinishReason:
    if has_tool_calls:
        return "tool_calls"
    if refused:
        return "content_filter"
    if data.get("status") == "incomplete":
        details = data.get("incomplete_details") or {}
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "content_filter"
        return "other"
    return "stop" if data.get("status") in (None, "completed") else "other"


def _message_to_items(m: Message) -> list[dict[str, Any]]:
    if m.role == "tool":
        return [
            {"type": "function_call_output", "call_id": m.tool_call_id, "output": m.content or ""}
        ]
    items: list[dict[str, Any]] = []
    if m.role != "assistant" or m.content or not m.tool_calls:
        items.append({"role": m.role, "content": m.content or ""})
    for tc in m.tool_calls:
        items.append(
            {
                "type": "function_call",
                "call_id": tc.id,
                "name": tc.name,
                "arguments": json.dumps(tc.arguments),
            }
        )
    return items


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    try:
        args = json.loads(raw["arguments"]) if raw.get("arguments") else {}
        if not isinstance(args, dict):
            raise TypeError
        return ToolCall(id=str(raw["call_id"]), name=str(raw["name"]), arguments=args)
    except (KeyError, TypeError, ValueError):
        raise MalformedResponseError("도구 호출 형식이 올바르지 않다") from None


def _safe_code(code: object) -> str:
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return f" (code={code})"
    return ""


def _body_error_code(data: Any) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    return _safe_code(err.get("code")) if isinstance(err, dict) else ""


def _error_code(resp: httpx.Response) -> str:
    try:
        return _body_error_code(resp.json())
    except ValueError:
        return ""
