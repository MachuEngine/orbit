import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from orbit.core.config import LLMSettings
from orbit.llm.factory import build_llm, llm_config_info
from orbit.llm.interface import (
    LLMRequestError,
    MalformedResponseError,
    ModelMismatchError,
    TransientLLMError,
)
from orbit.llm.model_id import InvalidModelIdError
from orbit.llm.openai import OpenAILLM
from orbit.llm.openrouter import OpenRouterLLM
from orbit.llm.types import ForcedToolChoice, LLMRequest, Message, ToolCall, ToolSpec

MODEL = "openai/gpt-6-luna"
KEY = "sk-test-SYNTHETIC-KEY-123"  # gitleaks:allow (합성 테스트 값)
PRICES = {
    "llm_price_input_per_mtok": 0.10,
    "llm_price_cached_input_per_mtok": 0.01,
    "llm_price_output_per_mtok": 0.50,
    "llm_price_source": "test-table",
}


def _settings(**over: Any) -> LLMSettings:
    values: dict[str, Any] = {
        "llm_provider": "openai",
        "llm_model_id": MODEL,
        "openai_api_key": KEY,
        "llm_max_retries": 2,
    }
    values.update(over)
    return LLMSettings(**values)


def _ok_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "completed",
        "model": "gpt-6-luna",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": "안녕"}]},
        ],
        "usage": {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 400},
            "output_tokens": 200,
            "output_tokens_details": {"reasoning_tokens": 150},
        },
    }
    body.update(over)
    return body


def _request(**over: Any) -> LLMRequest:
    values: dict[str, Any] = {
        "messages": (
            Message(role="system", content="규칙"),
            Message(role="user", content="합성 질문"),
        ),
        "purpose": "system_under_test",
        "tools": (
            ToolSpec(
                name="list_events",
                description="일정 조회",
                parameters={"type": "object", "properties": {"day": {"type": "string"}}},
            ),
        ),
        "tool_choice": "auto",
        "max_output_tokens": 100,
        "temperature": 0.0,
    }
    values.update(over)
    return LLMRequest(**values)


class Server:
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


def _llm(server: Server, **over: Any) -> OpenAILLM:
    return OpenAILLM(_settings(**over), server.transport(), sleep=_no_sleep)


async def test_request_shape() -> None:
    server = Server(_json(200, _ok_body()))
    await _llm(server, llm_reasoning_effort="low").complete(_request())
    (req,) = server.requests
    assert str(req.url) == "https://api.openai.com/v1/responses"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["model"] == "gpt-6-luna"  # 접두 제거
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_output_tokens"] == 100
    assert body["tool_choice"] == "auto"
    assert body["input"] == [
        {"role": "system", "content": "규칙"},
        {"role": "user", "content": "합성 질문"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "name": "list_events",
            "description": "일정 조회",
            "parameters": {"type": "object", "properties": {"day": {"type": "string"}}},
        }
    ]
    assert "temperature" not in body  # 기본은 보내지 않는다


async def test_temperature_sent_only_when_enabled() -> None:
    server = Server(_json(200, _ok_body()))
    llm = _llm(server, llm_send_temperature=True)
    await llm.complete(_request())
    assert json.loads(server.requests[0].content)["temperature"] == 0.0
    assert llm.fingerprint_info["temperature_sent"] is True
    off = _llm(Server(), llm_send_temperature=False)
    assert off.fingerprint_info == {
        "provider": "openai",
        "reasoning_effort": None,
        "temperature_sent": False,
        "price_source": None,
    }
    assert "reasoning" not in off.build_body(_request())


async def test_effort_none_is_sent_and_forced_tool_choice() -> None:
    llm = _llm(Server(), llm_reasoning_effort="none")
    body = llm.build_body(_request(tool_choice=ForcedToolChoice("list_events")))
    assert body["reasoning"] == {"effort": "none"}
    assert body["tool_choice"] == {"type": "function", "name": "list_events"}
    assert llm.build_body(_request(tool_choice="none", tools=()))["tool_choice"] == "none"
    assert "tools" not in llm.build_body(_request(tools=()))


async def test_tool_round_trip_items() -> None:
    call = ToolCall(id="call_1", name="list_events", arguments={"day": "2026-01-02"})
    messages = (
        Message(role="user", content="질문"),
        Message(role="assistant", content=None, tool_calls=(call,)),
        Message(role="tool", content='{"events": []}', tool_call_id="call_1"),
        Message(role="assistant", content="중간 설명", tool_calls=(call,)),
        Message(role="tool", content="x", tool_call_id="call_1"),
    )
    items = _llm(Server()).build_body(_request(messages=messages))["input"]
    args = json.dumps({"day": "2026-01-02"})
    assert items == [
        {"role": "user", "content": "질문"},
        {"type": "function_call", "call_id": "call_1", "name": "list_events", "arguments": args},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"events": []}'},
        {"role": "assistant", "content": "중간 설명"},
        {"type": "function_call", "call_id": "call_1", "name": "list_events", "arguments": args},
        {"type": "function_call_output", "call_id": "call_1", "output": "x"},
    ]


async def test_parse_text_usage_and_cost() -> None:
    server = Server(_json(200, _ok_body()))
    resp = await _llm(server, **PRICES).complete(_request())
    assert resp.text == "안녕"
    assert resp.tool_calls == ()
    assert resp.finish_reason == "stop"
    assert resp.model_id == "openai/gpt-6-luna"
    assert resp.provider == "openai"
    assert resp.usage.input_tokens == 1000
    assert resp.usage.cached_input_tokens == 400
    assert resp.usage.output_tokens == 200
    assert resp.usage.reasoning_tokens == 150
    # (600 * 0.10 + 400 * 0.01 + 200 * 0.50) / 1e6. 추론 토큰은 output에 포함돼 있어 한 번만 센다.
    assert resp.cost_usd == pytest.approx((60 + 4 + 100) / 1_000_000)


async def test_cost_is_none_without_prices() -> None:
    resp = await _llm(Server(_json(200, _ok_body()))).complete(_request())
    assert resp.cost_usd is None
    assert resp.cost_incomplete is False


async def test_parse_tool_call() -> None:
    body = _ok_body(
        output=[
            {
                "type": "function_call",
                "call_id": "call_9",
                "name": "list_events",
                "arguments": '{"day": "2026-01-02"}',
            }
        ]
    )
    resp = await _llm(Server(_json(200, body))).complete(_request())
    assert resp.text is None
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls == (ToolCall("call_9", "list_events", {"day": "2026-01-02"}),)


async def test_bad_tool_arguments_is_malformed() -> None:
    body = _ok_body(
        output=[{"type": "function_call", "call_id": "c", "name": "n", "arguments": "{"}]
    )
    with pytest.raises(MalformedResponseError):
        await _llm(Server(_json(200, body))).complete(_request())


async def test_incomplete_maps_to_length() -> None:
    body = _ok_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    resp = await _llm(Server(_json(200, body))).complete(_request())
    assert resp.finish_reason == "length"


async def test_model_mismatch_keeps_usage_and_model() -> None:
    body = _ok_body(model="gpt-6.1-terra")
    with pytest.raises(ModelMismatchError) as info:
        await _llm(Server(_json(200, body)), **PRICES).complete(_request())
    assert info.value.response_model_id == "openai/gpt-6.1-terra"
    assert info.value.usage.input_tokens == 1000
    assert info.value.cost_usd is not None


async def test_date_suffix_and_accepted_list() -> None:
    dated = _ok_body(model="gpt-6-luna-2026-11-01")
    resp = await _llm(Server(_json(200, dated))).complete(_request())
    assert resp.model_id == "openai/gpt-6-luna-2026-11-01"  # 러너가 선언 모델과 다르게 기록한다
    other = _ok_body(model="gpt-6-luna-preview")
    with pytest.raises(ModelMismatchError):
        await _llm(Server(_json(200, other))).complete(_request())
    ok = await _llm(
        Server(_json(200, other)), llm_accepted_response_model_ids=["openai/gpt-6-luna-preview"]
    ).complete(_request())
    assert ok.text == "안녕"


async def test_retry_on_429_and_5xx_then_success() -> None:
    server = Server(
        _json(429, {"error": {"code": "rate_limit_exceeded"}}),
        _json(503, {}),
        _json(200, _ok_body()),
    )
    resp = await _llm(server).complete(_request())
    assert resp.retries == 2
    assert len(server.requests) == 3


async def test_retry_on_timeout_and_connect_error() -> None:
    server = Server(httpx.ReadTimeout("t"), httpx.ConnectError("c"), _json(200, _ok_body()))
    resp = await _llm(server).complete(_request())
    assert resp.retries == 2


async def test_no_retry_on_4xx_and_message_has_no_body_or_key() -> None:
    server = Server(
        _json(400, {"error": {"code": "invalid_prompt", "message": f"echo 합성 질문 {KEY}"}})
    )
    with pytest.raises(LLMRequestError) as info:
        await _llm(server).complete(_request())
    assert len(server.requests) == 1
    text = str(info.value)
    assert "HTTP 400" in text
    assert "invalid_prompt" in text
    assert KEY not in text
    assert "합성 질문" not in text


async def test_retries_exhausted_error_has_no_key() -> None:
    server = Server(
        *[_json(500, {"error": {"code": "server_error", "message": KEY}}) for _ in range(3)]
    )
    with pytest.raises(TransientLLMError) as info:
        await _llm(server).complete(_request())
    assert info.value.retries == 2
    assert KEY not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


async def test_unsafe_error_code_is_dropped() -> None:
    server = Server(_json(400, {"error": {"code": f"bad code {KEY}"}}))
    with pytest.raises(LLMRequestError) as info:
        await _llm(server).complete(_request())
    assert "code=" not in str(info.value)


async def test_failed_status_and_garbage_body() -> None:
    failed = _ok_body(status="failed", error={"code": "server_error", "message": KEY})
    server = Server(*[_json(200, failed) for _ in range(3)])
    with pytest.raises(TransientLLMError) as info:
        await _llm(server).complete(_request())
    assert KEY not in str(info.value)
    with pytest.raises(MalformedResponseError):
        await _llm(Server(httpx.Response(200, text="not json"))).complete(_request())


def test_settings_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ORBIT_OPENAI_API_KEY", "ORBIT_OPENROUTER_API_KEY", "ORBIT_LLM_PROVIDER_ORDER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):  # openai인데 키 없음
        LLMSettings(llm_provider="openai", llm_model_id=MODEL)
    with pytest.raises(ValidationError):
        LLMSettings(llm_provider="openai", llm_model_id=MODEL, openai_api_key=" ")
    with pytest.raises(ValidationError):  # http
        _settings(openai_base_url="http://example.test/v1")
    with pytest.raises(ValidationError):  # 단가표 일부만
        _settings(llm_price_input_per_mtok=0.1)
    with pytest.raises(ValidationError):  # 출처 없음
        _settings(**{**PRICES, "llm_price_source": None})
    with pytest.raises(ValidationError):  # 음수 단가
        _settings(**{**PRICES, "llm_price_output_per_mtok": -1})
    with pytest.raises(ValidationError):  # 알 수 없는 강도
        _settings(llm_reasoning_effort="extreme")
    with pytest.raises(ValidationError):  # openrouter인데 키/순서 없음
        LLMSettings(llm_model_id="openai/gpt-4o-2024-08-06")
    # 쓰지 않는 provider의 키가 빈 값이어도(dev.env 기본) 통과한다
    _settings(openrouter_api_key="")
    ok = _settings(**PRICES)
    assert ok.openai_base_url == "https://api.openai.com/v1"
    assert ok.llm_send_temperature is False
    assert KEY not in repr(ok)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ORBIT_LLM_MODEL_ID", MODEL)
    monkeypatch.setenv("ORBIT_OPENAI_API_KEY", KEY)
    monkeypatch.setenv("ORBIT_LLM_REASONING_EFFORT", "")
    monkeypatch.setenv("ORBIT_LLM_PRICE_INPUT_PER_MTOK", "0.10")
    monkeypatch.setenv("ORBIT_LLM_PRICE_CACHED_INPUT_PER_MTOK", "0.01")
    monkeypatch.setenv("ORBIT_LLM_PRICE_OUTPUT_PER_MTOK", "0.50")
    monkeypatch.setenv("ORBIT_LLM_PRICE_SOURCE", "table")
    monkeypatch.delenv("ORBIT_LLM_PROVIDER_ORDER", raising=False)
    s = LLMSettings()
    assert s.llm_reasoning_effort is None
    assert s.llm_price_output_per_mtok == 0.50


def test_openai_requires_openai_prefixed_id() -> None:
    with pytest.raises(InvalidModelIdError):
        OpenAILLM(_settings(llm_model_id="anthropic/claude-4-5"))
    with pytest.raises(InvalidModelIdError):
        OpenAILLM(_settings(llm_model_id="openai/gpt-latest"))


def test_build_llm_branches() -> None:
    assert isinstance(build_llm(_settings()), OpenAILLM)
    router = LLMSettings(
        llm_model_id="openai/gpt-4o-2024-08-06",
        llm_provider_order=["openai"],
        openrouter_api_key=KEY,
    )
    assert isinstance(build_llm(router), OpenRouterLLM)


def test_llm_config_info_has_no_secrets() -> None:
    info = llm_config_info(_settings(llm_reasoning_effort="low", **PRICES))
    assert info == {
        "provider": "openai",
        "reasoning_effort": "low",
        "temperature_sent": False,
        "price_source": "test-table",
    }
    assert KEY not in json.dumps(info)
