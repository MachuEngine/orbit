import dataclasses
import json
from typing import Any

import pytest

from orbit.assistant.prompt import PROMPT_VERSION, build_system_prompt
from orbit.llm.scripted import ScriptExhaustedError

from .conftest import DAY, NOW, TZ, make_assistant, provider_ids, tc

QUESTION = ["내일 일정 알려줘"]


def good_script() -> list[dict[str, Any]]:
    return [
        {"tool_calls": [tc("list_events", **DAY)]},
        {
            "text": "내일은 주간 회의와 점심이 있습니다.",
            "structured": {"kind": "event_set", "events": ["E1", "E2"]},
        },
    ]


async def test_loop_returns_text_and_converted_structured(make_connectors: Any) -> None:
    assistant, llm = make_assistant(make_connectors, good_script())
    text, structured = await assistant.run(QUESTION)
    assert text == "내일은 주간 회의와 점심이 있습니다."
    assert structured == {
        "kind": "event_set",
        "events": [
            {"calendar_id": "cal-alice-main", "event_id": "evalice001"},
            {"calendar_id": "cal-alice-main", "event_id": "evalice002"},
        ],
    }
    assert len(llm.requests) == 2
    first = llm.requests[0]
    assert first.purpose == "system_under_test"
    assert first.messages[0].role == "system" and first.messages[1].content == QUESTION[0]


async def test_model_input_never_contains_provider_ids(make_connectors: Any) -> None:
    script: list[dict[str, Any]] = [
        {"tool_calls": [tc("list_events", **DAY)]},
        {"tool_calls": [tc("get_event", handle="E1"), tc("search_mail", query="from:bob")]},
        {"tool_calls": [tc("read_mail", handle="M1")]},
        {"text": "끝", "structured": {"kind": "event", "event": "E1"}},
    ]
    assistant, llm = make_assistant(make_connectors, script)
    await assistant.run(QUESTION)
    serialized = json.dumps(
        [dataclasses.asdict(m) for r in llm.requests for m in r.messages], ensure_ascii=False
    )
    assert "E1" in serialized and "M1" in serialized
    for pid in provider_ids():
        assert pid not in serialized


async def test_bad_calls_return_errors_and_loop_continues(make_connectors: Any) -> None:
    script: list[dict[str, Any]] = [
        {"tool_calls": [tc("get_event", handle="E7")]},  # 모르는 핸들
        {"tool_calls": [tc("list_events", time_min="내일", time_max="모레")]},  # 잘못된 시각
        {"tool_calls": [tc("search_mail", query="a OR b")]},  # InvalidQuery
        {"tool_calls": [tc("submit_answer", kind="event", event="E7")]},  # 제출로 치지 않는다
        {"text": "모르겠습니다."},
    ]
    assistant, llm = make_assistant(make_connectors, script)
    text, structured = await assistant.run(QUESTION)
    assert (text, structured) == ("모르겠습니다.", None)
    tool_msgs = [m.content for m in llm.requests[-1].messages if m.role == "tool"]
    assert [json.loads(c or "")["error"] for c in tool_msgs] == [
        "unknown_handle",
        "invalid_time",
        "invalid_query",
        "unknown_handle",
    ]


async def test_invalid_submit_then_valid_submit(make_connectors: Any) -> None:
    script: list[dict[str, Any]] = [
        {"tool_calls": [tc("list_events", **DAY)]},
        {"tool_calls": [tc("submit_answer", kind="event", event="E9")]},
        {"text": "점심입니다.", "structured": {"kind": "event", "event": "E2"}},
    ]
    assistant, _ = make_assistant(make_connectors, script)
    text, structured = await assistant.run(QUESTION)
    assert structured == {
        "kind": "event",
        "event": {"calendar_id": "cal-alice-main", "event_id": "evalice002"},
    }
    assert text == "점심입니다."


async def test_on_step_counts_llm_and_tool_calls(make_connectors: Any) -> None:
    steps: list[str] = []
    assistant, _ = make_assistant(make_connectors, good_script(), steps=steps)
    await assistant.run(QUESTION)
    assert len(steps) == 2 + 2  # LLM 2회 + 도구 호출(list_events, submit_answer) 2회


async def test_max_turns_stops_loop(make_connectors: Any) -> None:
    script = [{"tool_calls": [tc("list_events", **DAY)]}] * 5
    assistant, llm = make_assistant(make_connectors, script, max_turns=3)
    assert await assistant.run(QUESTION) == ("", None)
    assert len(llm.requests) == 3


async def test_script_exhaustion_propagates(make_connectors: Any) -> None:
    assistant, _ = make_assistant(make_connectors, [{"tool_calls": [tc("list_events", **DAY)]}])
    with pytest.raises(ScriptExhaustedError):
        await assistant.run(QUESTION)


async def test_deterministic_across_runs(make_connectors: Any) -> None:
    results = []
    for _ in range(2):
        assistant, llm = make_assistant(make_connectors, good_script())
        out = await assistant.run(QUESTION)
        results.append((out, llm.requests))
    assert results[0] == results[1]


async def test_tools_given_to_model_have_no_write_tools(make_connectors: Any) -> None:
    assistant, llm = make_assistant(make_connectors, good_script())
    await assistant.run(QUESTION)
    names = {t.name for t in llm.requests[0].tools}
    assert names == {"list_events", "get_event", "search_mail", "read_mail", "submit_answer"}


def test_system_prompt_content() -> None:
    prompt = build_system_prompt(NOW, TZ)
    assert "2026-11-11T09:00:00+09:00" in prompt and "수요일" in prompt
    assert "2026-11-09" in prompt  # 이번 주 월요일
    for needle in (
        "월요일",
        "취소된 일정을 이미 제외",
        "모든 텍스트 필드",
        "external_content",
        "submit_answer",
        "event_set",
    ):
        assert needle in prompt
    assert PROMPT_VERSION


async def test_submit_without_text_asks_final_text_without_tools(make_connectors: Any) -> None:
    script: list[dict[str, Any]] = [
        {"tool_calls": [tc("list_events", **DAY)]},
        {"structured": {"kind": "event", "event": "E2"}},  # 텍스트 없음
        {"text": "점심입니다."},
    ]
    steps: list[str] = []
    assistant, llm = make_assistant(make_connectors, script, steps=steps)
    text, structured = await assistant.run(QUESTION)
    assert text == "점심입니다." and structured is not None
    last = llm.requests[-1]
    assert last.tools == () and last.tool_choice == "none"
    assert len(steps) == 3 + 2  # LLM 3회 + 도구 호출 2회 (최종 답 호출 포함)


async def test_text_with_submit_needs_no_extra_call(make_connectors: Any) -> None:
    assistant, llm = make_assistant(make_connectors, good_script())
    await assistant.run(QUESTION)
    assert len(llm.requests) == 2


async def test_requests_pin_temperature_zero(make_connectors: Any) -> None:
    assistant, llm = make_assistant(make_connectors, good_script())
    await assistant.run(QUESTION)
    assert all(r.temperature == 0 for r in llm.requests)


def test_sut_version_reflects_prompt_and_temperature() -> None:
    from orbit.evalrunner.suts import create_sut

    assert create_sut("assistant_v0").version == f"0+{PROMPT_VERSION}+temp0"
