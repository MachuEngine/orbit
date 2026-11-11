import pytest

from orbit.llm.interface import RecordingLLM, UsageRecorder
from orbit.llm.model_id import InvalidModelIdError, validate_model_id
from orbit.llm.scripted import ScriptedLLM, ScriptExhaustedError
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage, Message

ALLOWED = [
    "openai/gpt-4o-2024-08-06",
    "anthropic/claude-sonnet-4.5",
    "google/gemini-2.5-flash",
    "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-32b",
]
REJECTED = [
    "",
    "   ",
    " openai/gpt-4o-2024-08-06",
    "openai/gpt 4o",
    "gpt-4o-2024-08-06",  # provider 접두 없음
    "/gpt-4o",
    "openai/",
    "openai/latest",
    "openai/gpt-latest",
    "openai/gpt-4o:latest",
    "openai/GPT-LATEST",
    "openrouter/auto",
    "openai/gpt-4o:nitro",
    "openai/gpt-4o:floating",
    "openai/gpt-4o:online",
    "openai/gpt-mini",  # 숫자 없음
    "a/b/gpt-4",
]


@pytest.mark.parametrize("model_id", ALLOWED)
def test_validate_model_id_allows(model_id: str) -> None:
    assert validate_model_id(model_id) == model_id


@pytest.mark.parametrize("model_id", REJECTED)
def test_validate_model_id_rejects(model_id: str) -> None:
    with pytest.raises(InvalidModelIdError):
        validate_model_id(model_id)


def _response(text: str, model_id: str = "x/model-1") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        usage=LLMUsage(1, 2, 0),
        cost_usd=None,
        latency_ms=0,
        model_id=model_id,
        provider=None,
    )


def _request(text: str = "q") -> LLMRequest:
    return LLMRequest(messages=(Message(role="user", content=text),), purpose="judge")


async def test_scripted_returns_in_order_records_requests_and_exhausts() -> None:
    llm = ScriptedLLM([_response("a"), _response("b")])
    r1 = await llm.complete(_request("1"))
    r2 = await llm.complete(_request("2"))
    assert (r1.text, r2.text) == ("a", "b")
    assert [m.content for req in llm.requests for m in req.messages] == ["1", "2"]
    assert llm.remaining == 0
    with pytest.raises(ScriptExhaustedError, match="소진"):
        await llm.complete(_request("3"))
    assert len(llm.requests) == 3


async def test_scripted_can_raise_scripted_exception() -> None:
    llm = ScriptedLLM([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        await llm.complete(_request())


async def test_usage_recorder_has_no_raw_text() -> None:
    recorder = UsageRecorder()
    llm = RecordingLLM(ScriptedLLM([_response("SECRET-ANSWER-TEXT")]), recorder)
    await llm.complete(_request("SECRET-PROMPT-TEXT"))
    (record,) = recorder.records
    assert record.purpose == "judge"
    assert record.model_id == "x/model-1"
    assert (record.input_tokens, record.output_tokens) == (1, 2)
    dumped = repr(record) + repr(recorder.records)
    assert "SECRET-PROMPT-TEXT" not in dumped
    assert "SECRET-ANSWER-TEXT" not in dumped
    assert recorder.total_usage == LLMUsage(1, 2, 0)
    assert recorder.total_cost_usd is None  # 비용을 모르는 호출이 있으면 합계도 모른다


async def test_recording_llm_records_failure_without_message() -> None:
    recorder = UsageRecorder()
    llm = RecordingLLM(ScriptedLLM([]), recorder)
    with pytest.raises(ScriptExhaustedError):
        await llm.complete(_request())
    (record,) = recorder.records
    assert record.error_type == "ScriptExhaustedError"
    assert record.model_id is None
