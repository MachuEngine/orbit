"""생성된 종단 과제(v1)를 `runflow_v0`(실사용 경로)로 끝까지 돌려 정답 계산과 채점이 맞물리는지
확인한다 (ADR-0026 §4, ADR-0023). 실제 PostgreSQL(`orbit_test`)을 쓰고 LLM은 스크립트 응답이다.

유형(+시나리오 유형·파생 종류·갈래)마다 과제 하나씩, 스크립트 LLM이 정답을 그대로 제출·실행하게
한다. `runflow_v0`는 문서·쓰기·리마인더·다중 턴을 모두 지원하므로 전부 성공해야 한다. 승인 거절
과제는 쓰기를 시도해도 아무것도 바뀌지 않아야 성공이다. 데이터는 모두 합성 값이다.
"""

from collections.abc import Sequence
from typing import Any

from eval_tasks_support import oracle_script, real_suite, real_task_world, slim_dataset
from orbit.evaldata.schema import Task
from orbit.evalrunner.llm_script import scripted_llm_factory
from orbit.evalrunner.runflow_env import RunflowEnvironment
from orbit.evalrunner.runner import ExperimentSummary, run_experiment
from orbit.evalrunner.suts import create_sut


def _tag(task: Task, prefix: str) -> str:
    return next(t for t in task.tags if t.startswith(prefix))


def representatives(tasks: Sequence[Task]) -> list[Task]:
    """(유형, 시나리오 유형, 파생 종류, 갈래)마다 첫 과제."""
    seen: dict[tuple[str, ...], Task] = {}
    for t in sorted(tasks, key=lambda t: t.task_id):
        variant = next((x for x in t.tags if x.startswith("variant:")), "")
        fault = next((x for x in t.tags if x.startswith("fault:")), "")
        key = (_tag(t, "type:"), _tag(t, "scenario:"), _tag(t, "derived:"), variant, fault)
        seen.setdefault(key, t)
    return list(seen.values())


async def run_all(app_url: str, tasks: Sequence[Task]) -> ExperimentSummary:
    _, world = real_task_world()
    dev = [t.model_copy(update={"split": "dev"}) for t in tasks]
    dataset = slim_dataset(dev, world)
    script: dict[str, Any] = {}
    for t in dev:
        script[t.task_id] = await oracle_script(dataset.world_snapshot, t, runflow=True)
    return await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut("runflow_v0"),
        llm_factory=scripted_llm_factory(script),
        repeats=1,
        git_sha="test-sha",
        database_env=RunflowEnvironment(app_url),
    )


async def test_oracle_answers_succeed_on_runflow_for_every_task_type(app_url: str) -> None:
    tasks = representatives(real_suite().tasks)
    kinds = {_tag(t, "type:")[5:] for t in tasks}
    assert {
        "calendar",
        "mail",
        "write",
        "multi_turn",
        "document",
        "reminder",
        "mismatch",
        "unanswerable",
        "ambiguous",
    } <= kinds
    summary = await run_all(app_url, tasks)
    assert len(summary.results) == len(tasks)
    failures = [
        f"{r.task_id}: {r.failure_type} {r.detail}" for r in summary.results if not r.success
    ]
    assert not failures, failures


async def test_write_tasks_are_scored_by_final_state_and_reject_leaves_no_change(
    app_url: str,
) -> None:
    suite = real_suite()
    writes = [
        t
        for t in suite.tasks
        if _tag(t, "type:") == "type:write" and _tag(t, "derived:") == "derived:none"
    ]
    rejects = [t for t in writes if t.approval_policy == ["reject"]]
    approved = [t for t in writes if t.approval_policy is None]
    picked = [*approved[:4], *rejects[:2]]
    assert len(rejects) == 4 and len(picked) == 6
    summary = await run_all(app_url, picked)
    by_id = {r.task_id: r for r in summary.results}
    for t in approved[:4]:
        r = by_id[t.task_id]
        assert (r.success, r.failure_type) == (True, None), r.detail
        assert r.change_matches == (True,)
    for t in rejects[:2]:
        r = by_id[t.task_id]
        assert r.success is True and r.state_diff_match is True


async def test_fault_write_task_is_reconciled_without_duplicate(app_url: str) -> None:
    faults = [
        t
        for t in real_suite().tasks
        if "derived:fault" in t.tags and "fault:result_unknown" in t.tags
    ]
    picked = [t for t in faults if t.fault_plan and t.fault_plan["rules"][0]["tool"] == "create_event"]
    assert picked
    summary = await run_all(app_url, picked[:1])
    r = summary.results[0]
    assert (r.success, r.failure_type) == (True, None), r.detail
    assert r.approval_violations == 0
