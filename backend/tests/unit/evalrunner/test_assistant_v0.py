"""`assistant_v0` SUT를 러너·구조화 채점과 연결한다(ADR-0011). LLM은 `ScriptedLLM`만 쓴다."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from orbit.assistant.prompt import PROMPT_VERSION
from orbit.evalrunner.runner import ExperimentSummary
from orbit.evalrunner.suts import create_sut, sut_names
from orbit.evalrunner.task_source import Dataset, load_dataset

from .conftest import DATASET_DIR, call, run, step

ALICE_MAIN = "cal-alice-main"
LIST_DAY = call(
    "list_events", time_min="2026-11-12T00:00:00+09:00", time_max="2026-11-13T00:00:00+09:00"
)

EXPECTED: dict[str, dict[str, Any]] = {
    "a-set": {
        "kind": "event_set",
        "events": [
            {"calendar_id": ALICE_MAIN, "event_id": "evalice001"},
            {"calendar_id": ALICE_MAIN, "event_id": "evalice002"},
        ],
    },
    "a-event": {"kind": "event", "event": {"calendar_id": ALICE_MAIN, "event_id": "evalice002"}},
    "a-time": {
        "kind": "event_time",
        "event": {"calendar_id": ALICE_MAIN, "event_id": "evalice001"},
        "start": "2026-11-12T10:00:00+09:00",
    },
    "a-slot": {"kind": "time_slot", "start": "2026-11-12T13:00:00+09:00"},
}

SUBMITS: dict[str, dict[str, Any]] = {
    "a-set": {"kind": "event_set", "events": ["E2", "E1"]},
    "a-event": {"kind": "event", "event": "E2"},
    "a-time": {"kind": "event_time", "event": "E1", "start": "2026-11-12T01:00:00+00:00"},
    "a-slot": {"kind": "time_slot", "start": "2026-11-12T13:00:00+09:00"},
}


def make_dataset(tmp_path: Path) -> Dataset:
    root = tmp_path / "assistant-ds"
    root.mkdir()
    shutil.copy(DATASET_DIR / "world.json", root / "world.json")
    manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["counts"]["tasks"] = len(EXPECTED)  # 과제 수를 실제 개수에 맞춘다
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    rows = []
    for task_id, expected in EXPECTED.items():
        rows.append(
            {
                "task_id": task_id,
                "scenario_id": f"s-{task_id}",
                "cluster_id": f"c-{task_id}",
                "split": "dev",
                "as_of_time": "2026-11-11T09:00:00+09:00",
                "account_id": "acct-alice",
                "turns": [{"role": "user", "content": f"합성 질문 {task_id}"}],
                "gold": {
                    "answer_facts": [],
                    "must_not_contain": [],
                    "expected_changes": [],
                    "required_tool_calls": [],
                    "forbidden_tools": ["create_event", "update_event", "cancel_event"],
                    "expected_answer": expected,
                },
                "difficulty": {"level": 1},
                "tags": ["calendar", "read"],
            }
        )
    (root / "tasks.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return load_dataset(root)


def script(submits: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        tid: [step(calls=[LIST_DAY]), {**step("짧은 답"), "structured": sub}]
        for tid, sub in submits.items()
    }


def outcome(summary: ExperimentSummary) -> dict[str, tuple[bool | None, str | None]]:
    return {r.task_id: (r.success, r.failure_type) for r in summary.results}


async def test_four_answer_kinds_are_scored_as_match(tmp_path: Path) -> None:
    summary = await run(make_dataset(tmp_path), script(SUBMITS), sut="assistant_v0", repeats=1)
    assert outcome(summary) == {tid: (True, None) for tid in EXPECTED}
    assert all(r.checks["structured_answer"] is True for r in summary.results)


async def test_wrong_handle_answer_is_wrong_answer_not_format(tmp_path: Path) -> None:
    subs = {**SUBMITS, "a-event": {"kind": "event", "event": "E1"}}
    summary = await run(make_dataset(tmp_path), script(subs), sut="assistant_v0", repeats=1)
    assert outcome(summary)["a-event"] == (False, "wrong_answer")


async def test_unknown_handle_then_retry_succeeds(tmp_path: Path) -> None:
    bad = {"kind": "event", "event": "E9"}
    scr = script(SUBMITS)
    scr["a-event"] = [
        step(calls=[LIST_DAY]),
        step(calls=[call("submit_answer", **bad)]),  # 오류가 돌아오고 루프가 계속된다
        {**step("점심입니다."), "structured": SUBMITS["a-event"]},
    ]
    summary = await run(make_dataset(tmp_path), scr, sut="assistant_v0", repeats=1)
    assert outcome(summary)["a-event"] == (True, None)


async def test_no_submit_is_answer_format(tmp_path: Path) -> None:
    scr = script(SUBMITS)
    scr["a-set"] = [step(calls=[LIST_DAY]), step("일정이 있습니다.")]
    summary = await run(make_dataset(tmp_path), scr, sut="assistant_v0", repeats=1)
    assert outcome(summary)["a-set"] == (False, "answer_format")


async def test_deterministic_across_runs(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path)
    a = await run(dataset, script(SUBMITS), sut="assistant_v0", repeats=2)
    b = await run(dataset, script(SUBMITS), sut="assistant_v0", repeats=2)
    assert [(r.task_id, r.repeat, r.success, r.failure_type, r.trajectory) for r in a.results] == [
        (r.task_id, r.repeat, r.success, r.failure_type, r.trajectory) for r in b.results
    ]


async def test_step_budget_applies_to_assistant(tmp_path: Path) -> None:
    from orbit.evalrunner.episode import Budget

    summary = await run(
        make_dataset(tmp_path),
        script(SUBMITS),
        sut="assistant_v0",
        repeats=1,
        budget=Budget(max_steps=2),
    )
    assert {r.failure_type for r in summary.results} == {"budget_exceeded"}


def test_sut_registered_with_prompt_version_in_fingerprint() -> None:
    assert "assistant_v0" in sut_names()
    sut = create_sut("assistant_v0")
    assert sut.name == "assistant_v0" and PROMPT_VERSION in sut.version


async def test_result_config_records_sut_version(tmp_path: Path) -> None:
    summary = await run(make_dataset(tmp_path), script(SUBMITS), sut="assistant_v0", repeats=1)
    assert PROMPT_VERSION in json.dumps(summary.results[0].config["sut"])


def test_step_counter_counts_observed_llm_and_connector_calls() -> None:
    from orbit.connectors.sim.calls import CallLog
    from orbit.evalrunner.episode import Budget, BudgetExceeded, StepCounter
    from orbit.llm.interface import UsageRecord, UsageRecorder

    recorder, log = UsageRecorder(), CallLog()
    counter = StepCounter(Budget(max_steps=3), recorder, log)
    assert counter.steps == 0 and counter.has_room()
    recorder.record(UsageRecord("system_under_test", None, None, 0, 0, 0, None, None, 0))
    log.record("list_events", {}, outcome="ok")
    assert counter.steps == 2 and counter.has_room()
    counter.consume()  # 확인만 한다. 상태가 바뀌지 않는다
    assert counter.steps == 2 and counter.verify() is None
    log.record("list_events", {}, outcome="ok")
    assert counter.steps == 3 and not counter.has_room()
    with pytest.raises(BudgetExceeded):
        counter.consume()
    assert counter.verify() is None  # 한도와 같으면 초과가 아니다
    log.record("get_event", {}, outcome="ok")
    assert counter.verify() == "steps"


NO_TEXT = {
    tid: [step(calls=[LIST_DAY]), {**step(None), "structured": sub}] for tid, sub in SUBMITS.items()
}


async def test_final_text_call_skipped_at_step_limit_keeps_submission(tmp_path: Path) -> None:
    from orbit.evalrunner.episode import Budget

    # 단계 = LLM 2회 + 커넥터 3회(list_calendars + 캘린더 2개 조회) = 5. 보충 호출은 6번째다.
    summary = await run(
        make_dataset(tmp_path), NO_TEXT, sut="assistant_v0", repeats=1, budget=Budget(max_steps=5)
    )
    assert outcome(summary) == {tid: (True, None) for tid in EXPECTED}  # budget_exceeded 아님
    assert all(r.llm_calls == 2 for r in summary.results)


async def test_final_text_call_runs_when_step_room_exists(tmp_path: Path) -> None:
    from orbit.evalrunner.episode import Budget

    scr = {tid: [*steps, step("짧은 답")] for tid, steps in NO_TEXT.items()}
    summary = await run(
        make_dataset(tmp_path), scr, sut="assistant_v0", repeats=1, budget=Budget(max_steps=6)
    )
    assert outcome(summary) == {tid: (True, None) for tid in EXPECTED}
    assert all(r.llm_calls == 3 for r in summary.results)
