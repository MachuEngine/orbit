"""`runflow_v0` 결과 행의 실행 기록(ADR-0029 §1): 턴별 발화·답 문장·도구 호출·핸들 대응표·
필드별 일치가 실제 PostgreSQL 경로에서 채워지는지 본다. LLM은 스크립트 응답이고
데이터는 합성이다."""

import json
from collections.abc import Sequence
from typing import Any

from eval_tasks_support import oracle_script, real_suite, real_task_world, slim_dataset
from orbit.evaldata.schema import Task
from orbit.evalrunner import metrics_v1 as m
from orbit.evalrunner.llm_script import scripted_llm_factory
from orbit.evalrunner.runflow_env import RunflowEnvironment
from orbit.evalrunner.runner import run_experiment
from orbit.evalrunner.suts import create_sut


def pick(*tags: str) -> Task:
    return next(
        t
        for t in sorted(real_suite().tasks, key=lambda t: t.task_id)
        if all(x in t.tags for x in tags)
    )


async def run_rows(app_url: str, tasks: Sequence[Task]) -> list[dict[str, Any]]:
    _, world = real_task_world()
    dev = [t.model_copy(update={"split": "dev"}) for t in tasks]
    dataset = slim_dataset(dev, world)
    script: dict[str, Any] = {}
    for t in dev:
        script[t.task_id] = await oracle_script(dataset.world_snapshot, t, runflow=True)
    summary = await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut("runflow_v0"),
        llm_factory=scripted_llm_factory(script),
        repeats=1,
        git_sha="test-sha",
        database_env=RunflowEnvironment(app_url),
    )
    return [json.loads(r.to_json_line()) for r in summary.results]


async def test_runflow_rows_carry_trace_and_every_section_has_values(app_url: str) -> None:
    write = pick("type:write", "scenario:meeting_request", "derived:none")
    fault = pick("derived:fault", "fault:result_unknown", "scenario:meeting_request")
    multi = pick("type:multi_turn", "scenario:day_agenda")
    doc = pick("type:document", "derived:none")
    reply = pick("type:multi_turn", "scenario:mail_fact")
    rows = await run_rows(app_url, [write, fault, multi, doc, reply])
    by = {r["task_id"]: r for r in rows}
    assert all(r["outcome"]["success"] for r in rows), [r["outcome"] for r in rows]

    # 쓰기 과제: 도구 호출 인자와 결과, 필드별 일치
    w = by[write.task_id]
    assert w["result_schema"] == 2 and w["task_meta"]["type"] == "write"
    calls = w["trace"]["tool_calls"]
    created = next(c for c in calls if c["name"] == "create_event")
    assert created["arguments"]["title"] and created["turn"] == 0
    assert w["trace"]["turns"][0]["user"] == write.turns[0].content
    assert w["trace"]["turns"][-1]["assistant"] and w["trace"]["final_text"]
    (change,) = w["change_fields"]
    assert change["kind"] == "event_created" and all(change["fields"].values())
    assert "title" in change["fields"] and "time" in change["fields"]

    # 결함 주입(결과 불명): 호출 기록에 규칙 이름이 남는다
    f = by[fault.task_id]
    assert any(c["fault"] == "create_event:result_unknown" for c in f["calls"])
    assert f["task_fault"] == ["create_event:result_unknown"]

    # 다중 턴: 사용자 발화와 비서 답 문장이 턴마다 남고 구조화된 답은 마지막 턴에 있다
    mt = by[multi.task_id]["trace"]
    users = [u.content for u in multi.turns if u.role == "user"]
    assert [t["user"] for t in mt["turns"]] == users
    assert all(t["assistant"] for t in mt["turns"])
    assert mt["turns"][-1]["structured"]["kind"] == "event_set"
    assert mt["turns"][0]["structured"] is None
    assert mt["answer_handles"] and all(h.startswith("E") for h in mt["answer_handles"])

    # 문서 과제: 문서 핸들 대응표와 영역 계산
    d = by[doc.task_id]["trace"]
    searched = next(c for c in d["tool_calls"] if c["name"] == "search_docs")
    assert "files" in json.loads(searched["result"])  # 모델에 보여 준 결과 본문 그대로
    listed = next(c for c in mt["tool_calls"] if c["name"] == "list_events")
    assert "events" in json.loads(listed["result"])
    submit = next(c for c in mt["tool_calls"] if c["name"] == "submit_answer")
    assert json.loads(submit["result"]) == {"status": "submitted"}
    assert list(d["answer_handles"]) and all(h.startswith("D") for h in d["answer_handles"])

    # 모든 섹션이 값을 낸다
    out = m.extra_metrics(rows)
    assert out["accuracy_extra"]["write_fields"]["by_field"]["title"]["rate"] == 1.0
    assert out["accuracy_extra"]["multi_turn"]["episodes"] == 2
    assert out["grounding"]["fact_support"]["episodes"] == len(rows)
    assert out["grounding"]["citation"]["available"] is True
    assert out["tools"]["selection"]["micro_recall"] is not None
    assert out["tools"]["argument_validity"]["tool_calls"] > 0
    assert out["tools"]["excess_calls"]["available"] is True
    assert out["robustness"]["fault"]["episodes"] == 1
    assert out["efficiency"]["cost_per_success_usd"] is not None
