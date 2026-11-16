"""생성된 종단 과제(v1)의 정답 계산과 채점이 맞물리는지 `assistant_v0`로 확인한다 (ADR-0026 §4).

유형(+시나리오 유형·파생 종류)마다 과제 하나씩, 스크립트 LLM이 정답을 그대로 제출하게 해 끝까지
돌린다. 읽기·텍스트 과제는 성공해야 한다. `assistant_v0`가 지원하지 않는 유형(문서, 상태 변화를
기대하는 쓰기·리마인더 요청)은 실패하는 것이 정상이다.
승인 거절 과제는 기대 변화가 없어 성공한다.
실제 LLM은 부르지 않는다. 합성 데이터만 쓴다.
"""

from collections.abc import Sequence
from typing import Any

import pytest

from eval_tasks_support import oracle_script, real_suite, real_task_world, slim_dataset
from orbit.evaldata.schema import Task
from orbit.evalrunner.llm_script import scripted_llm_factory
from orbit.evalrunner.runner import ExperimentSummary, run_experiment
from orbit.evalrunner.suts import create_sut

UNSUPPORTED_GROUPS = {"document"}  # 쓰기·리마인더 요청은 기대 변화가 있는 과제로 따로 가른다


def _tag(task: Task, prefix: str) -> str:
    return next(t for t in task.tags if t.startswith(prefix))


def representatives(tasks: Sequence[Task]) -> list[Task]:
    """(유형, 시나리오 유형, 파생 종류, 갈래)마다 첫 과제."""
    seen: dict[tuple[str, ...], Task] = {}
    for t in sorted(tasks, key=lambda t: t.task_id):
        variant = next((x for x in t.tags if x.startswith("variant:")), "")
        key = (
            _tag(t, "type:"),
            _tag(t, "scenario:"),
            _tag(t, "derived:"),
            variant if "reminder" in t.tags[0] or "absent" in t.tags[1] else "",
        )
        seen.setdefault(key, t)
    return list(seen.values())


async def run_all(tasks: Sequence[Task]) -> ExperimentSummary:
    _, world = real_task_world()
    dev = [t.model_copy(update={"split": "dev"}) for t in tasks]
    dataset = slim_dataset(dev, world)
    script: dict[str, Any] = {}
    for t in dev:
        script[t.task_id] = await oracle_script(dataset.world_snapshot, t, runflow=False)
    return await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut("assistant_v0"),
        llm_factory=scripted_llm_factory(script),
        repeats=1,
        git_sha="test-sha",
    )


async def test_oracle_answers_score_as_expected_on_assistant_v0() -> None:
    tasks = representatives(real_suite().tasks)
    assert len(tasks) >= 30
    summary = await run_all(tasks)
    by_id = {t.task_id: t for t in tasks}
    unexpected: list[str] = []
    for r in summary.results:
        task = by_id[r.task_id]
        group = _tag(task, "type:")[5:]
        needs_write = bool(task.gold.expected_changes)
        unsupported = group in UNSUPPORTED_GROUPS or needs_write
        if unsupported and r.success:
            unexpected.append(f"{r.task_id}: 지원하지 않는 유형이 성공했다")
        if not unsupported and not r.success:
            unexpected.append(f"{r.task_id}: {r.failure_type} {r.detail}")
    assert not unexpected, unexpected


async def test_every_supported_group_is_covered_and_scored() -> None:
    tasks = representatives(real_suite().tasks)
    groups = {_tag(t, "type:")[5:] for t in tasks}
    assert {
        "calendar",
        "mail",
        "mismatch",
        "unanswerable",
        "ambiguous",
        "multi_turn",
        "reminder",
        "write",
        "document",
    } <= groups
    scenarios = {_tag(t, "scenario:")[9:] for t in tasks}
    assert {
        "day_agenda",
        "free_slot",
        "reschedule",
        "cancellation",
        "project_week",
        "person_next",
        "cross_midnight",
        "tz_event",
        "all_day",
    } <= scenarios


@pytest.mark.parametrize("derived", ["fault"])
async def test_fault_tasks_recover_after_one_retry(derived: str) -> None:
    faults = [t for t in real_suite().tasks if f"derived:{derived}" in t.tags]
    reads = [t for t in faults if t.fault_plan and t.fault_plan["rules"][0]["tool"] != "send_draft"]
    reads = [t for t in reads if not t.gold.expected_changes][:4]
    assert len(reads) == 4
    summary = await run_all(reads)
    assert all(r.success for r in summary.results), [
        (r.task_id, r.failure_type) for r in summary.results
    ]
    # 장애가 실제로 주입돼 첫 조회 호출이 오류로 끝났다
    for r in summary.results:
        errors = [
            c["error_type"] for c in r.calls if c["tool"] in ("list_events", "search_messages")
        ]
        assert errors[0] is not None and errors[-1] is None


async def test_scoring_rejects_a_changed_answer_so_success_is_not_vacuous() -> None:
    tasks = representatives(real_suite().tasks)
    day = next(t for t in tasks if "scenario:day_agenda" in t.tags and "derived:none" in t.tags)
    other = next(t for t in tasks if "scenario:project_week" in t.tags)
    _, world = real_task_world()
    dataset = slim_dataset([day.model_copy(update={"split": "dev"})], world)
    script = {day.task_id: await oracle_script(dataset.world_snapshot, day, runflow=False)}
    wrong_gold = day.gold.model_copy(update={"expected_answer": other.gold.expected_answer})
    wrong = day.model_copy(update={"split": "dev", "gold": wrong_gold})
    summary = await run_experiment(
        slim_dataset([wrong], world),
        split="dev",
        sut_factory=lambda: create_sut("assistant_v0"),
        llm_factory=scripted_llm_factory(script),
        repeats=1,
        git_sha="test-sha",
    )
    assert [(r.success, r.failure_type) for r in summary.results] == [(False, "wrong_answer")]
