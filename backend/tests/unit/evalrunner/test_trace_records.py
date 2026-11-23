"""실행 기록과 보조 지표 연결 (ADR-0029 §1·§2): `assistant_v0`를 스크립트 LLM으로 돌린 결과 행에
새 필드가 실제로 채워지고, 모든 섹션이 값을 내는지 본다. 합성 데이터만 쓴다."""

import json
from collections.abc import Sequence
from typing import Any

from eval_tasks_support import oracle_script, real_suite, real_task_world, slim_dataset
from orbit.evaldata.schema import Task
from orbit.evalrunner import metrics_v1 as m
from orbit.evalrunner.llm_script import scripted_llm_factory
from orbit.evalrunner.runner import run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.trace import MAX_RESULT_CHARS, EpisodeTrace, ToolCallTrace, build_trace


def first(tasks: Sequence[Task], *tags: str, scenario: str | None = None) -> Task:
    for t in sorted(tasks, key=lambda t: t.task_id):
        if all(x in t.tags for x in tags) and (
            scenario is None or f"scenario:{scenario}" in t.tags
        ):
            return t
    raise LookupError(tags)


def paraphrase_of(task: Task) -> Task:
    tags = [("derived:paraphrase" if x.startswith("derived:") else x) for x in task.tags]
    turns = [t.model_copy(update={"content": t.content + " 부탁해"}) for t in task.turns]
    return task.model_copy(update={"task_id": task.task_id + "-para", "tags": tags, "turns": turns})


async def run_rows(tasks: Sequence[Task]) -> list[dict[str, Any]]:
    _, world = real_task_world()
    dev = [t.model_copy(update={"split": "dev"}) for t in tasks]
    dataset = slim_dataset(dev, world)
    script: dict[str, Any] = {}
    for t in dev:
        script[t.task_id] = await oracle_script(
            dataset.world_snapshot, t.model_copy(update={"task_id": t.task_id}), runflow=False
        )
    summary = await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut("assistant_v0"),
        llm_factory=scripted_llm_factory(script),
        repeats=1,
        git_sha="test-sha",
    )
    return [json.loads(r.to_json_line()) for r in summary.results]


async def test_assistant_rows_carry_trace_and_every_section_has_values() -> None:
    tasks = real_suite().tasks
    plain = first(tasks, "type:calendar", "derived:none", scenario="day_agenda")
    fault = first(tasks, "derived:fault", "type:calendar", scenario="day_agenda")
    mail = first(tasks, "type:mail", "derived:none")
    gone = first(tasks, "type:unanswerable")
    multi = first(tasks, "type:multi_turn", scenario="day_agenda")
    picked = [plain, paraphrase_of(plain), fault, mail, gone, multi]
    rows = await run_rows(picked)
    by = {r["task_id"]: r for r in rows}

    for r in rows:
        assert r["result_schema"] == 2
        assert r["min_tool_calls"] is not None
        assert r["task_meta"]["type"] and r["task_meta"]["as_of"]
    assert by[plain.task_id]["task_meta"]["refs"]["event"]
    assert by[plain.task_id + "-para"]["task_meta"]["origin_id"] == plain.task_id

    # 턴별 발화·답 문장·구조화된 답 원본
    t = by[plain.task_id]["trace"]
    assert [x["user"] for x in t["turns"]] == [plain.turns[0].content]
    assert t["final_text"] and t["turns"][-1]["assistant"] == t["final_text"]
    assert t["structured"]["kind"] == by[plain.task_id]["answer_ids"]["kind"]
    users = [x["user"] for x in by[multi.task_id]["trace"]["turns"]]
    assert users == [u.content for u in multi.turns if u.role == "user"] and len(users) >= 2

    # 도구 호출별 결과 본문·오류 종류, 답 핸들 -> 아티팩트 ID 대응표
    names = [c["name"] for c in t["tool_calls"]]
    assert "list_events" in names and names[-1] == "submit_answer"
    listing = next(c for c in t["tool_calls"] if c["name"] == "list_events")
    assert listing["result"].startswith("{") and listing["error_kind"] is None
    assert listing["result_chars"] == len(listing["result"]) and not listing["result_truncated"]
    handles = t["answer_handles"]
    assert handles and all(h[0] == "E" and len(ref) == 2 for h, ref in handles.items())

    # 결함 주입: 첫 커넥터 호출이 장애로 실패하고, 호출 기록에 규칙 이름·지연이 남는다
    f = by[fault.task_id]
    faulted = [c for c in f["calls"] if c["fault"]]
    assert fault.fault_plan is not None
    rule = fault.fault_plan["rules"][0]
    assert faulted and faulted[0]["fault"] == f"list_events:{rule['error']}"
    assert faulted[0]["outcome"] == "error" and isinstance(faulted[0]["latency_ms"], float)
    kinds = [c["error_kind"] for c in f["trace"]["tool_calls"] if c["is_error"]]
    assert kinds and kinds[0] in {"transient_error", "rate_limited"}
    assert by[plain.task_id]["calls"][0]["fault"] is None

    out = m.extra_metrics(rows)
    assert out["grounding"]["fact_support"]["available"] is True
    assert out["grounding"]["citation"]["episodes"] >= 2
    assert out["grounding"]["abstention"]["abstain_rate"] == 1.0
    # 기권 과제는 도구를 쓰지 않아 기대 영역(메일)을 놓친 것으로 센다
    assert 0 < out["tools"]["selection"]["micro_recall"] < 1
    assert out["tools"]["argument_validity"]["validity_rate"] is not None
    assert out["tools"]["excess_calls"]["available"] is True
    assert out["robustness"]["fault"]["episodes"] == 1
    assert out["robustness"]["paraphrase"]["pairs"] == 1
    assert out["accuracy_extra"]["multi_turn"]["episodes"] == 1
    eff = out["efficiency"]
    assert eff["cost_per_success_usd"] is not None and eff["latency_ms_p95"] is not None
    assert eff["tool_calls_per_episode"] > 0


def test_tool_result_longer_than_cap_is_truncated_with_marker() -> None:
    collector = EpisodeTrace()
    long = "가" * (MAX_RESULT_CHARS + 5)
    collector.record_tool("read_mail", {"handle": "M1"}, long, is_error=False)
    collector.record_tool("list_events", {}, '{"error": "invalid_time"}', is_error=True)
    trace = build_trace(
        user_turns=["질문"], final_text="답", structured=None, collector=collector, transcript=None
    )
    first_call, second = trace["tool_calls"]
    assert len(first_call["result"]) == MAX_RESULT_CHARS
    assert first_call["result_chars"] == MAX_RESULT_CHARS + 5 and first_call["result_truncated"]
    assert second["error_kind"] == "invalid_time" and second["result_truncated"] is False


def test_answer_handle_table_uses_last_valid_submission_only() -> None:
    calls = [
        ToolCallTrace("submit_answer", {"kind": "event", "event": "E9"}, "err", True),
        ToolCallTrace("submit_answer", {"kind": "event_set", "events": ["E1", "e2"]}, "ok", False),
    ]
    from orbit.evalrunner.trace import answer_handle_table

    exported = {"events": {"E1": ["c", "ev1"], "E2": ["c", "ev2"], "E9": ["c", "ev9"]}}
    assert answer_handle_table(calls, exported) == {"E1": ["c", "ev1"], "E2": ["c", "ev2"]}
    assert answer_handle_table(calls[:1], exported) == {}
    assert answer_handle_table(calls, None) == {}
