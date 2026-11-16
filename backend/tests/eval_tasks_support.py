"""종단 과제 v1(`orbit.evaldata.v1.tasks`) 테스트 공용 도구. 데이터는 모두 합성 값이다.

- `real_task_world()`·`real_suite()`: 커밋된 세계 v1로 한 번 만든 설계도·과제(표현 변형 없이).
- `slim_dataset()`: 러너가 읽는 `Dataset`. 검색용 계정(메일 6,200통)은 빼서 에피소드 준비를 줄인다.
- `oracle_script()`: 과제의 정답을 그대로 제출하는 스크립트 LLM 응답. 도구 호출로 일정·메일·문서의
  핸들을 만든 뒤(미리 같은 도구를 실행해 핸들을 알아낸다) 정답 핸들로 제출한다. 정답 계산과 채점이
  맞물리는지 확인하는 용도다.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.schema import (
    Task,
)
from orbit.evaldata.v1.task_gold import Ctx
from orbit.evaldata.v1.task_world import TaskWorld, load_task_world
from orbit.evaldata.v1.tasks import DEFAULT_PLAN, Candidates, Suite, build_candidates, build_suite
from orbit.evalrunner.oracle import (
    Oracle as _Oracle,
)
from orbit.evalrunner.task_source import Dataset

COMMITTED_V1 = Path(__file__).resolve().parents[2] / "eval" / "datasets" / "orbit-eval" / "v1"
SEED = 20261116


@functools.lru_cache(maxsize=1)
def real_task_world() -> tuple[TaskWorld, dict[str, Any]]:
    return load_task_world(COMMITTED_V1)


@functools.lru_cache(maxsize=1)
def real_candidates() -> Candidates:
    tw, _ = real_task_world()
    return build_candidates(Ctx(tw, SEED))


@functools.lru_cache(maxsize=1)
def real_suite() -> Suite:
    tw, _ = real_task_world()
    suite, _ = build_suite(
        tw, SEED, DEFAULT_PLAN, include_paraphrase=False, candidates=real_candidates()
    )
    return suite


def slim_dataset(tasks: Sequence[Task], world: dict[str, Any]) -> Dataset:
    """e2e 계정만 남긴 세계로 러너용 `Dataset`을 만든다."""
    accounts = {k: v for k, v in world["accounts"].items() if not k.startswith("acct-s")}
    snapshot = {**world, "accounts": accounts}
    return Dataset("orbit-eval", "v1", {}, snapshot, tuple(tasks))


_WORLDS: dict[int, SimWorld] = {}


def _world_for(snapshot: Any, as_of: datetime) -> SimWorld:
    key = id(snapshot)
    if key not in _WORLDS:
        _WORLDS[key] = SimWorld.load(dict(snapshot), FixedClock(as_of))
    return _WORLDS[key].fork(0, FixedClock(as_of))


class Oracle(_Oracle):
    """러너와 같은 오라클(`orbit.evalrunner.oracle`). 테스트는 스냅샷 dict에서 세계를 만든다."""

    def __init__(self, snapshot: Any, task: Task) -> None:
        super().__init__(
            _world_for(snapshot, task.as_of_time), snapshot["accounts"][task.account_id], task
        )


def step(text: str | None = None, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "text": text,
        "tool_calls": calls or [],
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.0001,
    }


async def oracle_script(
    snapshot: Any, task: Task, *, runflow: bool, with_fault: bool = True
) -> list[dict[str, Any]]:
    """과제의 정답을 제출하는 스크립트 LLM 응답. `runflow`면 제출 뒤 마무리 응답을 하나 더 둔다."""
    o = Oracle(snapshot, task)
    changes = list(task.gold.expected_changes or [])
    steps: list[dict[str, Any]] = []
    structured: dict[str, Any] | None = None
    reads: list[dict[str, Any]] = []
    if task.gold.expected_answer is not None:
        reads, structured = await o.answer()
    pre: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    for change in changes:
        c, w = await o.write(change)
        pre += c
        writes.append(w)
    if len(task.turns) == 1:
        read_calls = reads or pre
        if read_calls:
            steps.append(step(calls=read_calls))
        if structured is not None:
            steps.append({**step(o.text()), "structured": structured})
            if runflow:
                steps.append(step("정리했습니다."))
        elif writes:
            steps.append(step(calls=writes))
            steps.append(step("처리했습니다."))
        else:
            steps.append(step(o.text()))
    else:
        steps += _multi_turn_steps(o, task, reads, pre, writes, structured, runflow)
    if with_fault and task.fault_plan:
        steps = _with_failed_first_read(steps, task)
    return steps


def _multi_turn_steps(
    o: Oracle,
    task: Task,
    reads: list[dict[str, Any]],
    pre: list[dict[str, Any]],
    writes: list[dict[str, Any]],
    structured: dict[str, Any] | None,
    runflow: bool,
) -> list[dict[str, Any]]:
    n = len(task.turns)
    if not runflow:  # assistant_v0는 모든 발화를 한 번에 받는다
        calls = reads or pre
        out = [step(calls=calls)] if calls else []
        out.append({**step(o.text()), "structured": structured} if structured else step(o.text()))
        return out
    out = []
    if structured is not None:  # 읽기 다중 턴: 첫 턴에서 조회하고 마지막 턴에서 제출한다
        out += [step(calls=reads), step("조회했습니다.")]
        out += [step("확인했습니다.") for _ in range(n - 2)]
        out += [{**step(o.text()), "structured": structured}, step("정리했습니다.")]
        return out
    out += [step(calls=pre), step("조회했습니다.")]  # 첫 턴: 대상을 찾는다
    out += [step("확인했습니다.") for _ in range(n - 2)]
    out += [step(calls=writes), step("처리했습니다.")]
    return out


def _with_failed_first_read(steps: list[dict[str, Any]], task: Task) -> list[dict[str, Any]]:
    """첫 조회 호출이 장애로 실패하므로 같은 호출 단계를 한 번 더 둔다(재시도)."""
    assert task.fault_plan is not None
    tool = task.fault_plan["rules"][0]["tool"]
    if tool not in ("list_events", "search_messages"):
        return steps
    for i, s in enumerate(steps):
        if any(c["name"] in ("list_events", "search_mail") for c in s["tool_calls"]):
            return [*steps[: i + 1], dict(s), *steps[i + 1 :]]
    return steps


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()
