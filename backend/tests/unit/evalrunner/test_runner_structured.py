"""구조화된 답 채점(ADR-0010 §1)이 러너·결과·통계에 반영되는지 확인한다. 정답 과제는 t-002다."""

import json
from typing import Any

from orbit.evalrunner import stats
from orbit.evalrunner.runner import EpisodeResult
from orbit.evalrunner.task_source import Dataset

from .conftest import call, good_script, run, step

RIGHT = {
    "kind": "event_set",
    "events": [{"calendar_id": "cal-alice-team", "event_id": "evalice003"}],
}
LIST_TEAM = call(
    "list_events",
    calendar_id="cal-alice-team",
    time_min="2026-11-13T00:00:00+09:00",
    time_max="2026-11-14T00:00:00+09:00",
)


def t2(summary: Any) -> EpisodeResult:
    result: EpisodeResult = next(r for r in summary.results if r.task_id == "t-002")
    return result


async def test_structured_answer_decides_success(dataset: Dataset) -> None:
    r = t2(await run(dataset, repeats=1))
    assert (r.success, r.failure_type) == (True, None)
    assert r.text_fact_recall == 1.0
    assert r.checks["structured_answer"] is True
    assert r.checks["answer_facts"] is None  # 성공 판정에 쓰지 않는다


async def test_wrong_structured_value_is_wrong_answer_even_if_text_is_right(
    dataset: Dataset,
) -> None:
    script = good_script()
    wrong = {
        "kind": "event_set",
        "events": [{"calendar_id": "cal-alice-main", "event_id": "evalice001"}],
    }
    script["t-002"] = [
        step(calls=[LIST_TEAM]),
        {**step("모레는 팀 회고가 있습니다."), "structured": wrong},
    ]
    r = t2(await run(dataset, script, repeats=1))
    assert (r.success, r.failure_type) == (False, "wrong_answer")
    assert r.text_fact_recall == 1.0  # 텍스트는 맞다. 성공에는 쓰이지 않는다


async def test_missing_structured_answer_is_answer_format(dataset: Dataset) -> None:
    script = good_script()
    script["t-002"] = [step(calls=[LIST_TEAM]), step("모레는 팀 회고가 있습니다.")]
    r = t2(await run(dataset, script, repeats=1))
    assert (r.success, r.failure_type) == (False, "answer_format")
    assert r.text_fact_recall == 1.0


async def test_malformed_structured_answer_is_answer_format(dataset: Dataset) -> None:
    script = good_script()
    bad = {**RIGHT, "events": [*RIGHT["events"], *RIGHT["events"]]}  # 중복 항목
    script["t-002"] = [step(calls=[LIST_TEAM]), {**step("팀 회고"), "structured": bad}]
    assert t2(await run(dataset, script, repeats=1)).failure_type == "answer_format"


async def test_text_fact_recall_is_a_fraction_and_none_for_text_tasks(dataset: Dataset) -> None:
    script = good_script()
    script["t-002"] = [step(calls=[LIST_TEAM]), {**step("일정이 있습니다."), "structured": RIGHT}]
    summary = await run(dataset, script, repeats=1)
    r = t2(summary)
    assert r.success is True and r.text_fact_recall == 0.0
    t1 = next(x for x in summary.results if x.task_id == "t-001")
    assert t1.text_fact_recall is None  # 텍스트 채점 과제
    row = json.loads(r.to_json_line())
    assert row["text_fact_recall"] == 0.0


async def test_must_not_contain_is_still_a_hard_failure(dataset: Dataset) -> None:
    script = good_script()
    script["t-002"] = [
        step(calls=[LIST_TEAM]),
        {**step("팀 회고와 점심이 있습니다."), "structured": RIGHT},
    ]
    assert t2(await run(dataset, script, repeats=1)).failure_type == "wrong_answer"


async def test_forbidden_tool_outranks_answer_format(dataset: Dataset) -> None:
    script = good_script()
    cancel = call("cancel_event", calendar_id="cal-alice-team", event_id="evalice003", if_match="x")
    script["t-002"] = [step(calls=[cancel]), step("끝")]
    assert t2(await run(dataset, script, repeats=1)).failure_type == "forbidden_tool"


async def test_sut_error_outranks_answer_format(dataset: Dataset) -> None:
    summary = await run(dataset, {}, repeats=1)  # 스크립트가 없어 LLM 호출이 실패한다
    assert t2(summary).failure_type == "sut_error"


async def test_distribution_separates_answer_format_and_wrong_answer(dataset: Dataset) -> None:
    rows: list[dict[str, Any]] = []
    for kind in ("format", "wrong"):
        script = good_script()
        if kind == "format":
            script["t-002"] = [step(calls=[LIST_TEAM]), step("팀 회고")]
        else:
            other = {"kind": "event_set", "events": []}
            script["t-002"] = [step(calls=[LIST_TEAM]), {**step("팀 회고"), "structured": other}]
        summary = await run(dataset, script, repeats=1, experiment_id=f"e-{kind}")
        rows += [r.to_dict() for r in summary.results if r.task_id == "t-002"]
    for i, row in enumerate(rows):
        row["repeat"] = i + 1  # 중복 검사를 피하려고 반복 번호만 다르게 한다
    dist = stats.failure_distribution(rows)
    assert dist == {"answer_format": 1, "wrong_answer": 1}


async def test_submit_answer_is_not_a_connector_call(dataset: Dataset) -> None:
    r = t2(await run(dataset, repeats=1))
    assert r.trajectory == ("list_events",)
