"""3a 리뷰 1회차 수정에 대한 테스트: 모델 ID 허용 규칙, 파싱 실패, 비용 합산, 예외 체인."""

import pickle
import traceback
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from orbit.core.config import LLMSettings
from orbit.llm.interface import (
    LLMError,
    LLMRequestError,
    MalformedResponseError,
    ModelMismatchError,
    RecordingLLM,
    RetryPolicy,
    TransientLLMError,
    UsageRecorder,
    run_with_retry,
)
from orbit.llm.types import LLMResponse, LLMUsage

from .test_openrouter import KEY, MODEL, Server, _json, _llm, _no_sleep, _ok_body, _request


@pytest.mark.parametrize("model", [MODEL, f"{MODEL}-2025-01-02", f"{MODEL}-20250102"])
async def test_accepts_exact_and_date_suffixed_response_model(model: str) -> None:
    resp = await _llm(Server(_json(200, _ok_body(model=model)))).complete(_request())
    assert resp.model_id == model


@pytest.mark.parametrize(
    "model",
    [f"{MODEL}-mini", f"{MODEL}-2025-1-2", f"{MODEL}-2025-01-02-x", "openai/gpt-4o-2024-11-20"],
)
async def test_rejects_other_response_model_and_records_its_id(model: str) -> None:
    recorder = UsageRecorder()
    llm = RecordingLLM(_llm(Server(_json(200, _ok_body(model=model)))), recorder)
    with pytest.raises(ModelMismatchError) as info:
        await llm.complete(_request())
    assert info.value.response_model_id == model
    (record,) = recorder.records
    assert record.model_id == model
    assert record.error_type == "ModelMismatchError"
    assert record.input_tokens == 10  # 응답을 받았으므로 사용량은 남는다
    assert record.cost_usd == pytest.approx(0.0012)


async def test_accepted_list_allows_alias_response_model() -> None:
    alias = "openai/gpt-4o-snapshot"
    server = Server(_json(200, _ok_body(model=alias)))
    resp = await _llm(server, llm_accepted_response_model_ids=[alias]).complete(_request())
    assert resp.model_id == alias


def test_accepted_list_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_LLM_MODEL_ID", MODEL)
    monkeypatch.setenv("ORBIT_LLM_PROVIDER_ORDER", "openai")
    monkeypatch.setenv("ORBIT_OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("ORBIT_LLM_ACCEPTED_RESPONSE_MODEL_IDS", "a/b-1, c/d-2")
    assert LLMSettings().llm_accepted_response_model_ids == ["a/b-1", "c/d-2"]
    monkeypatch.delenv("ORBIT_LLM_ACCEPTED_RESPONSE_MODEL_IDS")
    assert LLMSettings().llm_accepted_response_model_ids == []


async def test_missing_response_model_is_none_and_flagged_in_recorder() -> None:
    body = _ok_body()
    del body["model"]
    recorder = UsageRecorder()
    resp = await RecordingLLM(_llm(Server(_json(200, body))), recorder).complete(_request())
    assert resp.model_id is None
    (record,) = recorder.records
    assert record.model_id is None
    assert record.model_id_missing is True


@pytest.mark.parametrize(
    "body",
    [
        _ok_body(usage={"prompt_tokens": "abc"}),
        _ok_body(choices=[{"finish_reason": "stop", "message": "not-a-dict"}]),
        _ok_body(choices=[{"finish_reason": "stop", "message": {"tool_calls": ["x"]}}]),
        _ok_body(model=123),
        {"choices": [{"message": {"tool_calls": [{"id": 1, "function": {"arguments": "{"}}]}}]},
    ],
)
async def test_any_parse_failure_becomes_malformed_and_is_recorded(body: dict[str, Any]) -> None:
    recorder = UsageRecorder()
    server = Server(_json(200, body))
    with pytest.raises(MalformedResponseError):
        await RecordingLLM(_llm(server), recorder).complete(_request())
    assert len(server.requests) == 1  # 재시도하지 않는다
    assert recorder.records[0].error_type == "MalformedResponseError"


async def test_retry_sums_cost_and_marks_incomplete_when_unknown() -> None:
    calls = 0

    async def call() -> LLMResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientLLMError("t", usage=LLMUsage(7, 3, 0), cost_usd=0.25)
        if calls == 2:
            raise TransientLLMError("t")  # 비용 미상
        return LLMResponse("x", (), "stop", LLMUsage(10, 5, 0), 0.5, 0, MODEL, None)

    resp = await run_with_retry(call, RetryPolicy(max_retries=3), sleep=_no_sleep)
    assert resp.cost_usd == pytest.approx(0.75)
    assert resp.cost_incomplete is True
    assert resp.usage == LLMUsage(17, 8, 0)


async def test_retry_cost_complete_when_all_known() -> None:
    calls = 0

    async def call() -> LLMResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientLLMError("t", cost_usd=0.25)
        return LLMResponse("x", (), "stop", LLMUsage(), 0.5, 0, MODEL, None)

    resp = await run_with_retry(call, RetryPolicy(), sleep=_no_sleep)
    assert resp.cost_usd == pytest.approx(0.75)
    assert resp.cost_incomplete is False


async def test_total_cost_usd_is_none_when_any_call_cost_unknown() -> None:
    recorder = UsageRecorder()
    await RecordingLLM(_llm(Server(_json(200, _ok_body()))), recorder).complete(_request())
    assert recorder.total_cost_usd == pytest.approx(0.0012)

    bad = _llm(Server(_json(400, {})))
    with pytest.raises(LLMRequestError):
        await RecordingLLM(bad, recorder).complete(_request())
    assert recorder.total_cost_usd is None  # 실패 호출의 비용을 모른다
    assert recorder.known_cost_usd == pytest.approx(0.0012)
    assert recorder.records[1].cost_incomplete is True


def _server_for(name: str) -> Server:
    leaked = f"leak {KEY}"
    servers: dict[str, Server] = {
        "connect": Server(httpx.ConnectError(leaked)),
        "timeout": Server(httpx.ReadTimeout(leaked)),
        "4xx": Server(_json(401, {"error": {"code": 401, "message": leaked}})),
        "5xx": Server(_json(500, {"error": {"code": 500, "message": leaked}})),
        "parse": Server(_json(200, {"choices": [], "x": leaked})),
        "badtype": Server(_json(200, _ok_body(usage={"prompt_tokens": leaked}))),
        "mismatch": Server(_json(200, _ok_body(model="x/y-1"))),
    }
    return servers[name]


@pytest.mark.parametrize(
    "name", ["connect", "timeout", "4xx", "5xx", "parse", "badtype", "mismatch"]
)
async def test_error_has_no_chain_and_no_key_anywhere(name: str) -> None:
    with pytest.raises(LLMError) as info:
        await _llm(_server_for(name), llm_max_retries=0).complete(_request())
    exc = info.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    rendered = [
        repr(exc),
        str(exc),
        "".join(traceback.format_exception(exc)),
        pickle.dumps(exc).decode("latin-1"),
    ]
    for text in rendered:
        assert KEY not in text


def test_settings_reject_empty_key_and_non_https_url() -> None:
    with pytest.raises(ValidationError):
        LLMSettings(llm_model_id=MODEL, llm_provider_order=["openai"], openrouter_api_key="")
    with pytest.raises(ValidationError):
        LLMSettings(
            llm_model_id=MODEL,
            llm_provider_order=["openai"],
            openrouter_api_key=KEY,
            openrouter_base_url="http://example.test/v1",
        )
