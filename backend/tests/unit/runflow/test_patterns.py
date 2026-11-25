"""평가 전용 Agent 패턴 (ADR-0030) 단위 테스트. 데이터는 모두 합성 값이다."""

from typing import Any

import pytest

from orbit.evalrunner.cli import _parser
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.llm.interface import RecordingLLM, UsageRecorder
from orbit.llm.scripted import ScriptedLLM
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage, Message
from orbit.runflow.deps import RunDeps
from orbit.runflow.fingerprint import config_fingerprint
from orbit.runflow.patterns import (
    ALL_TOOL_NAMES,
    CATEGORIES,
    MAX_PLAN_LINE_CHARS,
    MAX_PLAN_LINES,
    OTHER,
    PATTERN_VERSIONS,
    PATTERNS,
    PatternMemory,
    clean_plan,
    parse_category,
)
from orbit.runflow.prompt import PROMPT_VERSION
from orbit.runflow.write_tools import run_tool_specs

INFO = {"provider": "openai", "reasoning_items_roundtrip": True}


def test_category_tables_use_real_tool_names() -> None:
    real = {spec.name for spec in run_tool_specs()}
    assert set(ALL_TOOL_NAMES) == real
    for name, category in CATEGORIES.items():
        assert category.tools <= real, name
        assert "conversation_context" in category.tools
    assert CATEGORIES[OTHER].tools == real and CATEGORIES[OTHER].max_steps is None
    write = {"create_event", "update_event_time", "cancel_event", "send_mail"}
    assert not CATEGORIES["일정 조회"].tools & write
    assert not CATEGORIES["메일 질의"].tools & write
    assert not CATEGORIES["문서 질의"].tools & write


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"category": "일정 조회"}', "일정 조회"),
        ('```json\n{"category":"리마인더"}\n```', "리마인더"),
        ('설명입니다 {"category": "메일 답장"} 끝', "메일 답장"),
        ('{"category": "없는 범주"}', None),
        ('{"category": 3}', None),
        ("일정 조회", None),
        ("", None),
        (None, None),
        ('{"category": ', None),
    ],
)
def test_parse_category(text: str | None, expected: str | None) -> None:
    assert parse_category(text) == expected


def test_clean_plan_caps_lines_and_length() -> None:
    raw = "\n\n".join(f"{i}. " + "가" * 500 for i in range(1, 20))
    lines = clean_plan(raw).splitlines()
    assert len(lines) == MAX_PLAN_LINES
    assert all(len(ln) <= MAX_PLAN_LINE_CHARS for ln in lines)
    assert clean_plan("  \n ") == "" and clean_plan(None) == ""


def test_allowed_tools_include_submit_only_after_classification() -> None:
    memory = PatternMemory()
    assert memory.allowed_tools("r1") is None
    memory.run("r1")
    assert memory.allowed_tools("r1") is None  # 분류 전
    memory.run("r1").category = "일정 조회"
    allowed = memory.allowed_tools("r1")
    assert allowed is not None and "submit_answer" in allowed and "send_mail" not in allowed


def test_fingerprint_unchanged_for_react_and_differs_per_pattern() -> None:
    for info in (None, INFO):
        plain = config_fingerprint("m", info, answer_extension=True)
        assert plain == config_fingerprint("m", info, answer_extension=True, agent_pattern=None)
        assert plain == config_fingerprint("m", info, answer_extension=True, agent_pattern="react")
        plan = config_fingerprint("m", info, answer_extension=True, agent_pattern="plan_execute")
        fixed = config_fingerprint("m", info, answer_extension=True, agent_pattern="fixed_workflow")
        assert len({plain, plan, fixed}) == 3


def test_sut_version_and_config_info_record_pattern() -> None:
    base = f"0+{PROMPT_VERSION}+eval-answer-v2+index-off"
    assert RunflowV0SUT().version == base
    assert RunflowV0SUT(pattern="react").version == base
    for name in ("plan_execute", "fixed_workflow"):
        sut = RunflowV0SUT(pattern=name)
        assert sut.version == f"{base}+{PATTERN_VERSIONS[name]}"
        info = sut.config_info(model_id="m", llm_info=dict(INFO))
        assert info["pattern"] == name and info["pattern_version"] == PATTERN_VERSIONS[name]
    react = RunflowV0SUT().config_info(model_id="m", llm_info=dict(INFO))
    assert "pattern" not in react and "pattern_version" not in react
    assert set(react) == {
        "runflow_fingerprint", "answer_extension", "variants", "index_mode",
        "max_approvals_per_run",
    }  # fmt: skip
    assert react["runflow_fingerprint"] == config_fingerprint(
        "m",
        INFO,
        answer_extension=True,
        reminder_approval=False,  # S0
    )
    with pytest.raises(ValueError):
        RunflowV0SUT(pattern="nope")


def test_cli_pattern_choices() -> None:
    parser = _parser()
    base = ["run", "--dataset", "d", "--split", "dev", "--sut", "runflow_v0", "--out", "o"]
    assert parser.parse_args(base).pattern is None
    for name in PATTERNS:
        assert parser.parse_args([*base, "--pattern", name]).pattern == name
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--pattern", "nope"])


def test_run_deps_pattern_normalization() -> None:
    none: Any = None
    kwargs: dict[str, Any] = dict.fromkeys(
        ("db", "clock", "ids", "llm", "resolver", "tz", "checkpoint_conninfo"), none
    )
    assert RunDeps(**kwargs, pattern="react").pattern is None
    plan = RunDeps(**kwargs, pattern="plan_execute")
    assert plan.pattern == "plan_execute" and isinstance(plan.pattern_memory, PatternMemory)
    assert RunDeps(**kwargs).pattern_memory is None
    with pytest.raises(ValueError):
        RunDeps(**kwargs, pattern="nope")


async def test_recording_llm_keeps_step_kind() -> None:
    recorder = UsageRecorder()
    response = LLMResponse(
        text="x", tool_calls=(), finish_reason="stop", usage=LLMUsage(1, 1), cost_usd=0.0,
        latency_ms=0, model_id="m", provider=None,
    )  # fmt: skip
    llm = RecordingLLM(ScriptedLLM([response, response]), recorder)
    msgs = (Message(role="user", content="hi"),)
    await llm.complete(LLMRequest(messages=msgs, purpose="system_under_test"))
    await llm.complete(
        LLMRequest(messages=msgs, purpose="system_under_test", metadata={"step_kind": "plan"})
    )
    assert [r.step_kind for r in recorder.records] == [None, "plan"]
