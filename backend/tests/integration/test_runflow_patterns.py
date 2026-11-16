"""평가 전용 Agent 패턴 통합 테스트 (ADR-0030). 데이터는 모두 합성 값이다.

실제 PostgreSQL(`orbit_test`), 실제 LangGraph 체크포인터를 쓰고 LLM(`ScriptedLLM`)과 커넥터(Sim)만
대체한다. 스크립트 순서는 요청 순서다(계획·분류 호출도 한 요청이다).
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from integration.runflow_support import DAY, Harness
from integration.test_runflow_eval import only, read_gold, read_script, run, task
from orbit.assistant.envelope import boundary_for
from orbit.evalrunner.suts import create_sut
from orbit.llm.types import LLMRequest
from orbit.runflow.answer import ANSWER_PROMPT_PARAGRAPH
from orbit.runflow.patterns import CATEGORIES, PLAN_PROMPT, PatternMemory
from orbit.runflow.prompt import build_system_prompt
from orbit.runflow.write_tools import run_tool_specs

Make = Callable[..., Awaitable[Harness]]

PLAN_MARK = "SYNTH-PLAN-MARK-5d21"
REPLAN_MARK = "SYNTH-REPLAN-MARK-8b40"
MOVE = {
    "handle": "E2",
    "start": "2026-11-12T14:00:00+09:00",
    "end": "2026-11-12T15:00:00+09:00",
}
BAD_LIST = {"time_min": "not-a-time", "time_max": "also-bad"}


def use(h: Harness, pattern: str) -> PatternMemory:
    memory = PatternMemory()
    h.deps.pattern = pattern
    h.deps.pattern_memory = memory
    return memory


def kinds(h: Harness) -> list[str | None]:
    return [r.metadata.get("step_kind") for r in h.llm.requests]


def system_of(request: LLMRequest) -> str:
    content = request.messages[0].content
    assert isinstance(content, str)
    return content


def tool_names(request: LLMRequest) -> set[str]:
    return {spec.name for spec in request.tools}


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def category(name: str) -> dict[str, Any]:
    return {"text": json.dumps({"category": name}, ensure_ascii=False)}


async def stored_text(h: Harness, run_id: uuid.UUID) -> str:
    """이 run이 RLS 테이블·체크포인터에 남긴 내용 전체를 한 문자열로 모은다."""
    parts: list[str] = []
    readable = 0
    async with h.db.workspace_session(h.ws) as s:
        for sql in (
            "SELECT content::text FROM run_message WHERE workspace_id = :ws",
            "SELECT payload::text FROM run_event WHERE workspace_id = :ws",
            "SELECT args::text || provenance::text FROM tool_call WHERE workspace_id = :ws",
        ):
            parts += [str(r[0]) for r in await s.execute(text(sql), {"ws": h.ws})]
        for table, column in (
            ("checkpoints", "checkpoint::text::bytea"),
            ("checkpoint_blobs", "blob"),
            ("checkpoint_writes", "blob"),
        ):
            rows = (
                await s.execute(
                    text(f"SELECT {column} FROM public.{table} WHERE thread_id = :t"),  # noqa: S608
                    {"t": str(run_id)},
                )
            ).all()
            for (blob,) in rows:
                raw = bytes(blob) if blob is not None else b""
                readable += int(str(run_id).encode() in raw)
                parts.append(raw.decode("latin-1"))
    assert readable > 0  # 점검이 체크포인트 내용을 읽을 수 있다(대조군)
    return "\n".join(parts)


# ---------------------------------------------------------------- 기본값(react)은 불변


async def test_default_keeps_request_unchanged(make_harness: Make) -> None:
    h = await make_harness([{"text": "끝"}])
    assert h.deps.pattern is None and h.deps.pattern_memory is None
    run_id = await h.start("안녕")
    await h.work()
    request = h.llm.requests[0]
    base = build_system_prompt(h.clock.now(), h.deps.tz, boundary_for(f"run:{run_id}"))
    assert system_of(request) == base
    assert request.tools == run_tool_specs()
    assert request.metadata == {}


# ---------------------------------------------------------------- plan_execute


async def test_plan_execute_plans_once_without_tools_then_acts_with_plan(
    make_harness: Make,
) -> None:
    h = await make_harness(
        [
            {"text": f"1. list_events로 내일 일정 조회 {PLAN_MARK}\n2. 답변"},
            {"tool_calls": [call("list_events", **DAY)]},
            {"text": "내일 일정이 있습니다."},
        ]
    )
    memory = use(h, "plan_execute")
    run_id = await h.start("내일 일정 알려줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    plan, act1, act2 = h.llm.requests
    assert kinds(h) == ["plan", "act", "act"]
    assert plan.tools == () and plan.tool_choice is None
    assert system_of(plan) == PLAN_PROMPT
    assert [m.content for m in plan.messages[1:]] == ["내일 일정 알려줘"]
    base = build_system_prompt(h.clock.now(), h.deps.tz, boundary_for(f"run:{run_id}"))
    for act in (act1, act2):
        assert system_of(act).startswith(base + "\n[작업 계획]\n")
        assert PLAN_MARK in system_of(act)
        assert act.tools == run_tool_specs()  # 도구 목록은 react와 같다
    assert memory.stats.plan_calls == 1 and memory.stats.replans == 0


async def test_plan_execute_replans_once_after_tool_error(make_harness: Make) -> None:
    h = await make_harness(
        [
            {"text": f"1. list_events {PLAN_MARK}"},
            {"tool_calls": [call("list_events", **BAD_LIST)]},  # 오류
            {"text": f"1. 범위를 고쳐 list_events {REPLAN_MARK}"},  # 재계획
            {"tool_calls": [call("list_events", **BAD_LIST)]},  # 또 오류. 재계획은 한 번뿐
            {"text": "조회하지 못했습니다."},
        ]
    )
    memory = use(h, "plan_execute")
    run_id = await h.start("내일 일정 알려줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert kinds(h) == ["plan", "act", "replan", "act", "act"]
    replan = h.llm.requests[2]
    assert replan.tools == () and system_of(replan) == PLAN_PROMPT
    note = str(replan.messages[-1].content)
    assert note.startswith("[재계획]") and "list_events:" in note
    # 재계획 요청에는 사용자 발화와 서버가 만든 오류 종류만 있다(도구 결과 본문 없음)
    assert [m.role for m in replan.messages] == ["system", "user", "user"]
    assert REPLAN_MARK in system_of(h.llm.requests[3])
    assert PLAN_MARK not in system_of(h.llm.requests[3])
    assert REPLAN_MARK in system_of(h.llm.requests[4])
    assert memory.stats.plan_calls == 2 and memory.stats.replans == 1


async def test_plan_execute_does_not_replan_without_error(make_harness: Make) -> None:
    h = await make_harness(
        [
            {"text": "1. list_events"},
            {"tool_calls": [call("list_events", **DAY)]},
            {"tool_calls": [call("conversation_context")]},
            {"text": "끝"},
        ]
    )
    use(h, "plan_execute")
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert kinds(h) == ["plan", "act", "act", "act"]


async def test_plan_execute_keeps_write_approval_interrupt(make_harness: Make) -> None:
    h = await make_harness(
        [
            {"text": "1. list_events\n2. update_event_time"},
            {"tool_calls": [call("list_events", **DAY)]},
            {"tool_calls": [call("update_event_time", **MOVE)]},
            {"text": "옮겼습니다."},
        ]
    )
    use(h, "plan_execute")
    run_id = await h.start("점심을 2시로 옮겨줘")
    await h.work()
    assert await h.status(run_id) == "waiting_approval"
    assert h.write_calls() == []  # 승인 전에는 외부 쓰기가 없다
    approval_id = await h.approval_id(run_id)
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert h.write_calls() == ["update_event"]
    assert kinds(h) == ["plan", "act", "act", "act"]  # 승인 뒤에도 다시 계획하지 않는다


async def test_plan_execute_state_and_rows_exclude_plan_sentences(make_harness: Make) -> None:
    h = await make_harness(
        [
            {"text": f"1. list_events {PLAN_MARK}"},
            {"tool_calls": [call("list_events", **BAD_LIST)]},
            {"text": f"1. 다시 {REPLAN_MARK}"},
            {"text": "끝"},
        ]
    )
    use(h, "plan_execute")
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    stored = await stored_text(h, run_id)
    assert PLAN_MARK not in stored and REPLAN_MARK not in stored


async def test_plan_llm_failure_fails_run(make_harness: Make) -> None:
    h = await make_harness([])  # 계획 호출에서 스크립트가 소진된다
    use(h, "plan_execute")
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "failed"


# ---------------------------------------------------------------- fixed_workflow


async def test_fixed_workflow_classifies_once_and_limits_tools(make_harness: Make) -> None:
    h = await make_harness(
        [
            category("일정 조회"),
            {
                "tool_calls": [
                    call("send_mail", to=["a@example.net"], subject="s", body="b"),
                    call("list_events", **DAY),
                ]
            },
            {"tool_calls": [call("conversation_context")]},
            {"text": "끝"},
        ]
    )
    memory = use(h, "fixed_workflow")
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert kinds(h) == ["classify", "act", "act", "act"]  # 재분류 없음
    classify = h.llm.requests[0]
    assert classify.tools == () and "일정 조회" in system_of(classify)
    allowed = CATEGORIES["일정 조회"].tools
    for act in h.llm.requests[1:]:
        assert tool_names(act) == allowed  # 답 제출 도구는 answer_sink가 없어 빠진다
        assert "[작업 범주: 일정 조회]" in system_of(act)
    # 부분집합 밖 도구는 프롬프트가 아니라 서버가 막는다. 승인 요청도 만들지 않는다
    assert "tool_not_allowed" in repr(h.llm.requests[2].messages)
    assert await h.count("SELECT count(*) FROM approval") == 0
    assert await h.count("SELECT count(*) FROM tool_call") == 0
    assert h.write_calls() == []
    assert memory.stats.classify_total == 1 and memory.stats.classify_parse_failed == 0
    assert memory.stats.categories == {"일정 조회": 1}


async def test_fixed_workflow_submit_answer_works_with_allowed_list(make_harness: Make) -> None:
    h = await make_harness(
        [
            category("일정 조회"),
            {"tool_calls": [call("list_events", **DAY)]},
            {
                "text": "내일은 주간 회의와 점심이 있습니다.",
                "structured": {"kind": "event_set", "events": ["E1", "E2"]},
            },
            {"text": "정리했습니다."},
        ]
    )
    sink: list[dict[str, Any]] = []
    h.deps.answer_sink = sink.append
    use(h, "fixed_workflow")
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    allowed = CATEGORIES["일정 조회"].tools | {"submit_answer"}
    for act in h.llm.requests[1:]:
        assert tool_names(act) == allowed
    assert len(sink) == 1 and sink[0]["kind"] == "event_set"
    assert "tool_not_allowed" not in repr(h.llm.requests[3].messages)


async def test_plan_execute_answer_prompt_comes_after_plan_paragraph(make_harness: Make) -> None:
    h = await make_harness([{"text": f"1. 답변 {PLAN_MARK}"}, {"text": "끝"}])
    h.deps.answer_sink = lambda answer: None
    use(h, "plan_execute")
    await h.start("안녕")
    await h.work()
    system = system_of(h.llm.requests[1])
    assert system.index("[작업 계획]") < system.index(ANSWER_PROMPT_PARAGRAPH)
    assert system.endswith(ANSWER_PROMPT_PARAGRAPH)


async def test_fixed_workflow_disallowed_write_does_not_block_allowed_write(
    make_harness: Make,
) -> None:
    h = await make_harness(
        [
            category("일정 쓰기"),
            {"tool_calls": [call("list_events", **DAY)]},
            {
                "tool_calls": [
                    call("send_mail", to=["a@example.net"], subject="s", body="b"),
                    call("update_event_time", **MOVE),
                ]
            },
            {"text": "옮겼습니다."},
        ]
    )
    use(h, "fixed_workflow")
    run_id = await h.start("점심을 2시로 옮겨줘")
    await h.work()
    assert await h.status(run_id) == "waiting_approval"  # 승인 interrupt 유지
    assert h.write_calls() == []
    approval_id = await h.approval_id(run_id)
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert h.write_calls() == ["update_event"]
    assert "tool_not_allowed" in repr(h.llm.requests[3].messages)


async def test_fixed_workflow_parse_failure_falls_back_to_all_tools(make_harness: Make) -> None:
    h = await make_harness([{"text": "일정 조회입니다"}, {"text": "끝"}])
    memory = use(h, "fixed_workflow")
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    act = h.llm.requests[1]
    assert act.tools == run_tool_specs()  # 기타 = 모든 도구
    assert "[작업 범주: 기타]" in system_of(act)
    assert memory.stats.classify_total == 1 and memory.stats.classify_parse_failed == 1
    assert memory.stats.categories == {"기타": 1}


async def test_fixed_workflow_step_cap_fails_run(make_harness: Make) -> None:
    cap = CATEGORIES["리마인더"].max_steps
    assert cap is not None
    h = await make_harness(
        [
            category("리마인더"),
            *({"tool_calls": [call("conversation_context")]} for _ in range(cap)),
        ]
    )
    use(h, "fixed_workflow")
    run_id = await h.start("30분 뒤에 알려줘")
    await h.work()
    assert await h.status(run_id) == "failed"
    assert (await h.run_row(run_id))["error_kind"] == "max_steps"
    assert kinds(h) == ["classify", *["act"] * cap]


async def test_fixed_workflow_state_and_rows_exclude_classification(make_harness: Make) -> None:
    h = await make_harness([{"text": f'{{"category": "일정 조회"}} {PLAN_MARK}'}, {"text": "끝"}])
    use(h, "fixed_workflow")
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert PLAN_MARK not in await stored_text(h, run_id)


# ---------------------------------------------------------------- 러너 연결


async def test_runner_records_pattern_usage_and_stats(app_url: str) -> None:
    t = task("p-1", ["내일 일정이 뭐가 있나요"], gold=read_gold())
    script = [{"text": "1. list_events\n2. 답변"}, *read_script()]
    summary, _, factory = await run(
        app_url, [t], {"p-1": script},
        sut=lambda: create_sut("runflow_v0", pattern="plan_execute"),
    )  # fmt: skip
    r = only(summary)
    assert r.success, r.detail
    row = r.to_dict()
    sut_config = row["config"]["sut_config"]
    assert sut_config["pattern"] == "plan_execute"
    assert sut_config["pattern_version"] == "plan-execute-v1"
    assert row["config"]["sut"]["version"].endswith("+plan-execute-v1")
    assert row["usage"]["llm_calls"] == 4
    assert row["usage"]["llm_calls_by_kind"] == {"act": 3, "plan": 1}
    assert row["pattern_stats"]["plan_calls"] == 1 and row["pattern_stats"]["replans"] == 0
    seen = [q.metadata.get("step_kind") for q in factory.made["p-1"].requests]
    assert seen == ["plan", "act", "act", "act"]
    # 단계 종류와 계획 문장은 결과 파일 trace에만 남는다(ADR-0029 §1)
    steps = row["trace"]["pattern_steps"]
    assert [x["kind"] for x in steps] == ["plan", "act", "act", "act"]
    assert steps[0]["text"] == "1. list_events\n2. 답변" and steps[1]["text"] is None
    from orbit.evalrunner import metrics_v1

    assert metrics_v1.extra_metrics([row])["tools"]["argument_validity"]["available"] is True


async def test_runner_default_row_has_no_pattern_fields(app_url: str) -> None:
    t = task("p-2", ["내일 일정이 뭐가 있나요"], gold=read_gold())
    summary, _, _ = await run(app_url, [t], {"p-2": read_script()})
    row = only(summary).to_dict()
    assert "pattern_stats" not in row and "llm_calls_by_kind" not in row["usage"]
    assert "pattern" not in row["config"]["sut_config"]
    assert [x["kind"] for x in row["trace"]["pattern_steps"]] == []


async def test_runner_fixed_workflow_records_parse_failure_rate(app_url: str) -> None:
    t = task("p-3", ["내일 일정이 뭐가 있나요"], gold=read_gold())
    script = [{"text": "분류 못함"}, *read_script()]
    summary, _, _ = await run(
        app_url, [t], {"p-3": script},
        sut=lambda: create_sut("runflow_v0", pattern="fixed_workflow"),
    )  # fmt: skip
    r = only(summary)
    assert r.success, r.detail
    row = r.to_dict()
    stats = row["pattern_stats"]
    assert stats["classify_total"] == 1 and stats["classify_parse_failed"] == 1
    assert stats["categories"] == {"기타": 1}
    assert row["usage"]["llm_calls_by_kind"] == {"act": 3, "classify": 1}
    assert [x["kind"] for x in row["trace"]["pattern_steps"]][0] == "classify"
    assert row["trace"]["pattern_steps"][0]["text"] == "분류 못함"
