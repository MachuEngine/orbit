"""`assistant_v0` SUT를 러너·구조화 채점과 연결한다(ADR-0011). LLM은 `ScriptedLLM`만 쓴다."""

import json
import shutil
from pathlib import Path
from typing import Any

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
    for name in ("manifest.json", "world.json"):
        shutil.copy(DATASET_DIR / name, root / name)
    rows = []
    for task_id, expected in EXPECTED.items():
        rows.append(
            {
                "task_id": task_id,
                "scenario_id": f"s-{task_id}",
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
