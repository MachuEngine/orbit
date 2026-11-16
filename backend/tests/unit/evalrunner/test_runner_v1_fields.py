"""과제별 시간대·장애 주입·쓰기 상태 채점의 러너 연결 (ADR-0026 §4·§7). `ScriptedLLM`만 쓴다."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from orbit.connectors.sim.faults import FaultPlan
from orbit.evaldata.dataset import DatasetError, load_dataset, write_dataset
from orbit.evalrunner import stats
from orbit.evalrunner.llm_script import ScriptedLLMFactory
from orbit.evalrunner.runner import EpisodeResult, FaultPlanSpec, run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import Dataset
from orbit.evalrunner.task_source import load_dataset as load_runner_dataset
from orbit.llm.scripted import ScriptedLLM

from .conftest import DATASET_DIR, call, step

MAIN = "cal-alice-main"
CREATE = call(
    "create_event",
    calendar_id=MAIN,
    event_id="evabc001",
    draft={
        "title": "합성 점검 회의",
        "start": "2026-11-14T10:00:00+09:00",
        "end": "2026-11-14T11:00:00+09:00",
    },
)
EXPECT_CREATE = {
    "kind": "event_created",
    "calendar_id": MAIN,
    "start": "2026-11-14T10:00:00+09:00",
    "end": "2026-11-14T11:00:00+09:00",
    "title_any_of": ["점검 회의"],
}
LIST_DAY = call(
    "list_events",
    calendar_id=MAIN,
    time_min="2026-11-12T00:00:00+09:00",
    time_max="2026-11-13T00:00:00+09:00",
)


def task_row(task_id: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "task_id": task_id,
        "scenario_id": f"s-{task_id}",
        "cluster_id": f"c-{task_id}",
        "split": "dev",
        "as_of_time": "2026-11-11T09:00:00+09:00",
        "account_id": "acct-alice",
        "turns": [{"role": "user", "content": f"합성 요청 {task_id}"}],
        "gold": {
            "answer_facts": [],
            "must_not_contain": [],
            "expected_changes": [],
            "required_tool_calls": [],
            "forbidden_tools": [],
        },
        "difficulty": {"level": 1},
        "tags": ["write"],
    }
    gold_over = over.pop("gold", {})
    row["gold"].update(gold_over)
    row.update(over)
    return row


def build(tmp_path: Path, *rows: dict[str, Any]) -> Path:
    root = tmp_path / "ds"
    root.mkdir(parents=True)
    shutil.copy(DATASET_DIR / "world.json", root / "world.json")
    manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["counts"]["tasks"] = len(rows)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (root / "tasks.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return root


class Spy(ScriptedLLMFactory):
    def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(script)
        self.made: dict[str, ScriptedLLM] = {}

    def __call__(self, task: Any, repeat: int) -> ScriptedLLM:
        llm = super().__call__(task, repeat)
        assert isinstance(llm, ScriptedLLM)
        self.made[task.task_id] = llm
        return llm


async def run(
    dataset: Dataset, script: dict[str, list[dict[str, Any]]], sut: str = "scripted_tool", **kw: Any
) -> tuple[list[EpisodeResult], Spy]:
    spy = Spy(script)
    summary = await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut(sut),
        llm_factory=spy,
        repeats=1,
        git_sha="test-sha",
        **kw,
    )
    return summary.results, spy


# ---------------------------------------------------------------- 쓰기 채점


async def test_write_task_success_and_failure(tmp_path: Path) -> None:
    ds = load_runner_dataset(
        build(
            tmp_path,
            task_row("w-ok", gold={"expected_changes": [EXPECT_CREATE]}),
            task_row("w-none", gold={"expected_changes": [EXPECT_CREATE]}),
            task_row("w-extra", gold={"expected_changes": []}),
        )
    )
    script = {
        "w-ok": [step(calls=[CREATE]), step("만들었습니다.")],
        "w-none": [step("만들지 않았습니다.")],
        "w-extra": [step(calls=[CREATE]), step("만들었습니다.")],
    }
    results, _ = await run(ds, script)
    by = {r.task_id: r for r in results}
    assert (by["w-ok"].success, by["w-ok"].failure_type) == (True, None)
    assert by["w-ok"].change_matches == (True,)
    assert by["w-ok"].to_dict()["change_matches"] == [True]
    assert (by["w-none"].success, by["w-none"].failure_type) == (False, "wrong_state_change")
    assert by["w-none"].change_matches == (False,)
    assert by["w-extra"].failure_type == "unexpected_state_change"
    assert by["w-extra"].change_matches is None
    # 값 원문은 행에 없다
    assert "점검 회의" not in by["w-ok"].to_json_line().split('"calls"')[0]
    summary = stats.write_task_summary([r.to_dict() for r in results])
    assert summary == {"episodes": 2, "successes": 1, "success_rate": 0.5}
    assert stats.failure_distribution([r.to_dict() for r in results]) == {
        "unexpected_state_change": 1,
        "wrong_state_change": 1,
    }


async def test_write_task_summary_is_none_without_write_tasks() -> None:
    row = {"outcome": {"success": True, "failure_type": None}, "change_matches": None}
    assert stats.write_task_summary([row]) is None


# ---------------------------------------------------------------- 시간대


async def test_default_time_zone_is_seoul_and_recorded(tmp_path: Path) -> None:
    ds = load_runner_dataset(build(tmp_path, task_row("z-kst")))
    results, spy = await run(ds, {"z-kst": [step("확인했습니다.")]}, sut="assistant_v0")
    assert results[0].time_zone == "Asia/Seoul"
    assert results[0].to_dict()["time_zone"] == "Asia/Seoul"
    assert "시간대 Asia/Seoul" in repr(spy.made["z-kst"].requests)


async def test_task_time_zone_reaches_assistant_prompt(tmp_path: Path) -> None:
    ds = load_runner_dataset(
        build(
            tmp_path,
            task_row("z-la", time_zone="America/Los_Angeles"),
            task_row("z-kst"),
        )
    )
    script = {
        "z-la": [step(calls=[LIST_DAY]), step("확인했습니다.")],
        "z-kst": [step("확인했습니다.")],
    }
    results, spy = await run(ds, script, sut="assistant_v0")
    by = {r.task_id: r for r in results}
    assert by["z-la"].time_zone == "America/Los_Angeles"
    la_requests = repr(spy.made["z-la"].requests)
    assert "시간대 America/Los_Angeles" in la_requests
    assert "2026-11-10T16:00:00-08:00" in la_requests  # as_of 09:00+09:00의 LA 현지 시각
    assert "Asia/Seoul" not in la_requests
    assert "시간대 Asia/Seoul" in repr(spy.made["z-kst"].requests)


async def test_time_zone_is_a_task_property_not_part_of_the_config_fingerprint(
    tmp_path: Path,
) -> None:
    plain = load_runner_dataset(build(tmp_path / "a", task_row("z-1")))
    la = load_runner_dataset(build(tmp_path / "b", task_row("z-1", time_zone="Europe/London")))
    ra, _ = await run(plain, {"z-1": [step("확인.")]}, sut="assistant_v0")
    rb, _ = await run(la, {"z-1": [step("확인.")]}, sut="assistant_v0")
    assert ra[0].config_fingerprint == rb[0].config_fingerprint
    assert (ra[0].time_zone, rb[0].time_zone) == ("Asia/Seoul", "Europe/London")


def test_invalid_time_zone_is_rejected_on_load(tmp_path: Path) -> None:
    root = build(tmp_path, task_row("z-bad", time_zone="Mars/Olympus"))
    with pytest.raises(DatasetError, match="time_zone"):
        load_dataset(root)


# ---------------------------------------------------------------- 과제별 장애 주입


FAULT_UNKNOWN = {"rules": [{"tool": "create_event", "error": "result_unknown"}]}


async def test_task_fault_plan_applies_per_episode(tmp_path: Path) -> None:
    ds = load_runner_dataset(
        build(
            tmp_path,
            task_row("f-1", fault_plan=FAULT_UNKNOWN, gold={"expected_changes": None}),
            task_row("f-2", gold={"expected_changes": None}),
        )
    )
    script = {t: [step(calls=[CREATE]), step("끝.")] for t in ("f-1", "f-2")}
    results, _ = await run(ds, script)
    by = {r.task_id: r for r in results}
    assert by["f-1"].calls[0]["outcome"] == "error"
    assert by["f-1"].calls[0]["error_type"] == "ResultUnknown"
    assert by["f-1"].task_fault_rules == ("create_event:result_unknown",)
    assert by["f-1"].to_dict()["task_fault"] == ["create_event:result_unknown"]
    assert by["f-2"].calls[0]["outcome"] == "ok"
    assert by["f-2"].to_dict()["task_fault"] is None


async def test_task_fault_plan_is_hidden_from_the_sut(tmp_path: Path) -> None:
    ds = load_runner_dataset(
        build(tmp_path, task_row("f-1", fault_plan=FAULT_UNKNOWN, time_zone="Asia/Seoul"))
    )
    _, spy = await run(ds, {"f-1": [step("끝.")]}, sut="assistant_v0")
    assert "result_unknown" not in repr(spy.made["f-1"].requests)


async def test_cli_fault_plan_with_task_fault_plan_is_rejected_before_running(
    tmp_path: Path,
) -> None:
    ds = load_runner_dataset(build(tmp_path, task_row("f-1", fault_plan=FAULT_UNKNOWN)))
    spy = Spy({})
    fault = FaultPlanSpec("cli", FaultPlan.from_dict({"rules": []}))
    with pytest.raises(ValueError, match="fault_plan"):
        await run_experiment(
            ds,
            split="dev",
            sut_factory=lambda: create_sut("noop"),
            llm_factory=spy,
            fault=fault,
            git_sha="t",
        )
    assert spy.made == {}  # 에피소드는 하나도 실행되지 않았다


def test_invalid_task_fault_plan_is_rejected_on_load(tmp_path: Path) -> None:
    root = build(
        tmp_path, task_row("f-bad", fault_plan={"rules": [{"tool": "x", "error": "boom"}]})
    )
    with pytest.raises(DatasetError, match="fault_plan"):
        load_dataset(root)


# ---------------------------------------------------------------- 데이터셋 검증·직렬화


@pytest.mark.parametrize(
    "change",
    [
        {**EXPECT_CREATE, "calendar_id": "cal-nope"},
        {
            "kind": "event_cancelled",
            "calendar_id": MAIN,
            "event_id": "evnope",
        },
        {
            "kind": "draft_created",
            "to_include": ["a@example.org"],
            "reply_to_message_id": "msg-nope",
        },
    ],
)
def test_expected_changes_must_reference_world_ids(tmp_path: Path, change: dict[str, Any]) -> None:
    root = build(tmp_path, task_row("w-bad", gold={"expected_changes": [change]}))
    with pytest.raises(DatasetError, match="world"):
        load_dataset(root)


def test_expected_changes_with_world_ids_load(tmp_path: Path) -> None:
    ok = [
        EXPECT_CREATE,
        {"kind": "event_cancelled", "calendar_id": MAIN, "event_id": "evalice001"},
        {
            "kind": "draft_created",
            "to_include": ["a@example.org"],
            "reply_to_message_id": "msg-a-001",
        },
    ]
    root = build(tmp_path, task_row("w-ok", gold={"expected_changes": ok}))
    assert len(load_dataset(root).tasks[0].gold.expected_changes or []) == 3


def test_write_dataset_omits_unset_v1_fields(tmp_path: Path) -> None:
    ds = load_dataset(DATASET_DIR)
    out = tmp_path / "out"
    write_dataset(out, ds.manifest, ds.world_dict, ds.tasks)
    text = (out / "tasks.jsonl").read_text(encoding="utf-8")
    for key in ("time_zone", "fault_plan", "answer_facts_required"):
        assert key not in text
    v0 = Path(__file__).resolve().parents[4] / "eval" / "datasets" / "orbit-eval" / "v0"
    ds0 = load_dataset(v0, include_excluded=True)
    out0 = tmp_path / "out0"
    write_dataset(out0, ds0.manifest, ds0.world_dict, ds0.tasks)
    assert (out0 / "tasks.jsonl").read_text("utf-8") == (v0 / "tasks.jsonl").read_text("utf-8")
