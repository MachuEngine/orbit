"""최소 호출 수(ADR-0029 §2): 정답 제출 오라클이 쓰는 도구 호출 수를 실행 시 계산한다.
러너가 결과 행의 `min_tool_calls`로 남기고, 테스트 전용 코드에 의존하지 않는다."""

from eval_tasks_support import real_suite, real_task_world, slim_dataset
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.schema import Task
from orbit.evalrunner import oracle
from orbit.evalrunner.runner import _min_tool_calls


def pick(*tags: str) -> Task:
    return next(t for t in real_suite().tasks if all(x in t.tags for x in tags))


async def floors_for(*tasks: Task) -> dict[str, int]:
    _, world = real_task_world()
    ds = slim_dataset([t.model_copy(update={"split": "dev"}) for t in tasks], world)
    base = SimWorld.load(dict(ds.world_snapshot), FixedClock(tasks[0].as_of_time))
    return await _min_tool_calls(ds.tasks, base, ds.world_snapshot)


async def test_floor_matches_the_oracle_script_call_count() -> None:
    read = pick("type:calendar", "derived:none", "scenario:day_agenda")
    mail = pick("type:mail", "derived:none")
    gone = pick("type:unanswerable")
    write = pick("type:write", "scenario:event_change_target", "derived:none")
    floors = await floors_for(read, mail, gone, write)
    assert floors[read.task_id] == 1  # 일정 조회 한 번
    assert floors[mail.task_id] == 1  # 검색 한 번
    assert floors[gone.task_id] == 0  # 기권은 조회가 필요 없다(오라클은 정답을 이미 안다)
    # 일정 변경: 대상 조회 한 번 + 쓰기 한 번(승인 거절 과제는 기대 변화가 없어 0일 수 있다)
    assert floors[write.task_id] in (0, 2)


async def test_floor_skips_tasks_that_the_oracle_cannot_build() -> None:
    task = pick("type:calendar", "derived:none", "scenario:day_agenda")
    missing = task.model_copy(update={"task_id": "t-x", "account_id": "acct-none", "split": "dev"})
    _, world = real_task_world()
    ds = slim_dataset([task.model_copy(update={"split": "dev"}), missing], world)
    base = SimWorld.load(dict(ds.world_snapshot), FixedClock(task.as_of_time))
    out = await _min_tool_calls(ds.tasks, base, ds.world_snapshot)
    assert set(out) == {task.task_id}


async def test_oracle_calls_split_reads_and_writes() -> None:
    write = next(
        t
        for t in real_suite().tasks
        if "type:write" in t.tags and t.gold.expected_changes and "derived:none" in t.tags
    )
    _, world = real_task_world()
    ds = slim_dataset([write.model_copy(update={"split": "dev"})], world)
    base = SimWorld.load(dict(ds.world_snapshot), FixedClock(write.as_of_time))
    forked = base.fork(0, clock=FixedClock(write.as_of_time))
    account = ds.world_snapshot["accounts"][write.account_id]
    reads, pre, writes = await oracle.oracle_calls(forked, account, write)
    assert reads == [] and len(writes) == len(write.gold.expected_changes or [])
    assert all(
        c["name"] in {"create_event", "update_event_time", "cancel_event", "send_mail"}
        for c in writes
    )
    assert pre or writes[0]["name"] == "create_event"
