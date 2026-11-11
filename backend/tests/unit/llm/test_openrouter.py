import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from orbit.core.config import LLMSettings
from orbit.llm.interface import (
    LLMRequestError,
    LLMTimeoutError,
    ModelMismatchError,
    RecordingLLM,
    RetryPolicy,
    TransientLLMError,
    UsageRecorder,
)
from orbit.llm.model_id import InvalidModelIdError
from orbit.llm.openrouter import OpenRouterLLM
from orbit.llm.types import ForcedToolChoice, LLMRequest, Message, ToolCall, ToolSpec

MODEL = "openai/gpt-4o-2024-08-06"
KEY = "sk-or-test-SYNTHETIC-KEY-123"  # gitleaks:allow (합성 테스트 값)


def _settings(**over: Any) -> LLMSettings:
    values: dict[str, Any] = {
        "llm_model_id": MODEL,
        "llm_provider_order": ["openai"],
        "openrouter_api_key": KEY,
        "llm_max_retries": 2,
    }
    values.update(over)
    return LLMSettings(**values)


def _ok_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": MODEL,
        "provider": "OpenAI",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "안녕"}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 4},
            "cost": 0.0012,
        },
    }
    body.update(over)
    return body


def _request() -> LLMRequest:
    return LLMRequest(
        messages=(Message(role="user", content="합성 질문"),),
        purpose="system_under_test",
        tools=(
            ToolSpec(
                name="list_events",
                description="일정 조회",
                parameters={"type": "object", "properties": {"day": {"type": "string"}}},
            ),
        ),
        tool_choice=ForcedToolChoice("list_events"),
        max_output_tokens=100,
        temperature=0.0,
    )


class Server:
    """요청을 기록하고 미리 정한 응답/예외를 순서대로 돌려주는 모의 전송."""

    def __init__(self, *items: httpx.Response | Exception) -> None:
        self.items = list(items)
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            item = self.items.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return httpx.MockTransport(handler)


def _json(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


async def _no_sleep(_: float) -> None:
    return None


def _llm(server: Server, **over: Any) -> OpenRouterLLM:
    return OpenRouterLLM(_settings(**over), server.transport(), sleep=_no_sleep)


async def test_request_body_has_routing_tools_and_auth() -> None:
    server = Server(_json(200, _ok_body()))
    await _llm(server).complete(_request())
    (req,) = server.requests
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["model"] == MODEL
    assert body["provider"] == {
        "order": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
    }
    assert body["tools"][0]["function"]["name"] == "list_events"
    assert body["tools"][0]["function"]["parameters"]["type"] == "object"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "list_events"}}
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.0
    assert body["messages"] == [{"role": "user", "content": "합성 질문"}]


async def test_assistant_tool_calls_and_tool_result_are_serialized() -> None:
    server = Server(_json(200, _ok_body()))
    call = ToolCall(id="c1", name="list_events", arguments={"day": "2026-01-02"})
    request = LLMRequest(
        messages=(
            Message(role="assistant", tool_calls=(call,)),
            Message(role="tool", content="[]", tool_call_id="c1"),
        ),
        purpose="judge",
    )
    await _llm(server).complete(request)
    msgs = json.loads(server.requests[0].content)["messages"]
    assert msgs[0]["tool_calls"][0]["function"]["arguments"] == '{"day": "2026-01-02"}'
    assert msgs[1]["tool_call_id"] == "c1"


async def test_parses_text_usage_and_cost() -> None:
    server = Server(_json(200, _ok_body()))
    resp = await _llm(server).complete(_request())
    assert resp.text == "안녕"
    assert resp.finish_reason == "stop"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (10, 5)
    assert resp.usage.cached_input_tokens == 4
    assert resp.cost_usd == pytest.approx(0.0012)
    assert resp.model_id == MODEL
    assert resp.provider == "OpenAI"
    assert resp.retries == 0
    assert resp.latency_ms >= 0


async def test_parses_tool_calls() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_events", "arguments": '{"day": "2026-01-02"}'},
            }
        ],
    }
    body = _ok_body(choices=[{"finish_reason": "tool_calls", "message": message}])
    resp = await _llm(Server(_json(200, body))).complete(_request())
    assert resp.text is None
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls == (ToolCall("call_1", "list_events", {"day": "2026-01-02"}),)


async def test_cost_missing_is_none_not_zero() -> None:
    body = _ok_body(usage={"prompt_tokens": 1, "completion_tokens": 1})
    resp = await _llm(Server(_json(200, body))).complete(_request())
    assert resp.cost_usd is None
    assert resp.usage.cached_input_tokens == 0


async def test_model_mismatch_raises_and_is_not_retried() -> None:
    server = Server(_json(200, _ok_body(model="openai/gpt-4o-mini-2024-07-18")))
    with pytest.raises(ModelMismatchError):
        await _llm(server).complete(_request())
    assert len(server.requests) == 1


async def test_provider_mismatch_raises() -> None:
    server = Server(_json(200, _ok_body(provider="Anthropic")))
    with pytest.raises(ModelMismatchError):
        await _llm(server).complete(_request())


async def test_retries_429_then_succeeds_and_sums_usage_and_latency() -> None:
    server = Server(_json(429, {"error": {"code": 429}}), _json(200, _ok_body()))
    resp = await _llm(server).complete(_request())
    assert len(server.requests) == 2
    assert resp.retries == 1
    assert resp.text == "안녕"


async def test_retry_sums_usage_from_failed_attempts() -> None:
    from orbit.llm.interface import run_with_retry
    from orbit.llm.types import LLMResponse, LLMUsage

    calls = 0

    async def call() -> LLMResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientLLMError("t", usage=LLMUsage(7, 3, 0))
        return LLMResponse("x", (), "stop", LLMUsage(10, 5, 4), 0.5, 0, MODEL, None)

    resp = await run_with_retry(call, RetryPolicy(max_retries=2), sleep=_no_sleep)
    assert resp.usage == LLMUsage(17, 8, 4)
    assert resp.retries == 1


async def test_5xx_retries_until_limit_then_fails() -> None:
    server = Server(*[_json(503, {}) for _ in range(3)])
    with pytest.raises(TransientLLMError) as info:
        await _llm(server).complete(_request())
    assert len(server.requests) == 3  # 첫 시도 + 재시도 2회
    assert info.value.retries == 2


async def test_4xx_fails_immediately() -> None:
    server = Server(_json(400, {"error": {"code": 400, "message": "합성 질문 되돌림"}}))
    with pytest.raises(LLMRequestError) as info:
        await _llm(server).complete(_request())
    assert len(server.requests) == 1
    assert "HTTP 400" in str(info.value)
    assert "합성 질문" not in str(info.value)


async def test_timeout_is_retried_then_raises_timeout_error() -> None:
    server = Server(*[httpx.ReadTimeout("slow") for _ in range(2)])
    with pytest.raises(LLMTimeoutError):
        await _llm(server, llm_max_retries=1).complete(_request())
    assert len(server.requests) == 2


async def test_timeout_then_success() -> None:
    server = Server(httpx.ConnectTimeout("slow"), _json(200, _ok_body()))
    resp = await _llm(server).complete(_request())
    assert resp.retries == 1


def test_timeout_setting_is_passed_to_client() -> None:
    assert OpenRouterLLM(_settings(llm_timeout_seconds=7.5))._timeout == 7.5


async def test_api_key_not_in_exceptions_or_recorder() -> None:
    recorder = UsageRecorder()
    server = Server(_json(401, {"error": {"code": 401}}))
    llm = RecordingLLM(_llm(server), recorder)
    with pytest.raises(LLMRequestError) as info:
        await llm.complete(_request())
    assert KEY not in str(info.value)
    assert KEY not in repr(info.value)
    assert KEY not in repr(recorder.records)
    settings = _settings()
    assert KEY not in repr(settings)
    assert KEY not in str(settings)


async def test_key_not_in_connection_error() -> None:
    server = Server(httpx.ConnectError(f"failed with {KEY}"))
    with pytest.raises(TransientLLMError) as info:
        await _llm(server, llm_max_retries=0).complete(_request())
    assert KEY not in str(info.value)


def test_invalid_model_id_fails_at_construction() -> None:
    with pytest.raises(InvalidModelIdError):
        OpenRouterLLM(_settings(llm_model_id="openai/gpt-latest"))


def test_settings_parse_provider_order_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_LLM_MODEL_ID", MODEL)
    monkeypatch.setenv("ORBIT_LLM_PROVIDER_ORDER", "openai, azure")
    monkeypatch.setenv("ORBIT_OPENROUTER_API_KEY", KEY)
    s = LLMSettings()
    assert s.llm_provider_order == ["openai", "azure"]
    assert s.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert KEY not in repr(s)


def test_settings_require_key_and_nonempty_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_LLM_MODEL_ID", MODEL)
    monkeypatch.setenv("ORBIT_LLM_PROVIDER_ORDER", " , ")
    monkeypatch.delenv("ORBIT_OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        LLMSettings()
