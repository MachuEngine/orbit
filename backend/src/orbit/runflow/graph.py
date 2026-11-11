"""그래프 조립과 run 진행 (ADR-0003, ADR-0005 조건 3, ADR-0012 §4).

`advance_run`은 작업 처리기(`execute_run`·`resume_run`)가 공통으로 쓰는 멱등 진입점이다.
- run이 없으면(삭제됨) 정상 종료한다.
- 종료 상태 run은 아무것도 하지 않는다.
- `waiting_approval`인데 승인이 아직 `pending`이면 아무것도 하지 않는다. 승인 시각이 지났으면
  `expired`로 바꾼 뒤 이어서 진행한다(만료도 결정이다).
- 그 밖에는 체크포인트 상태를 보고 시작, 재개(`interrupt` 해제), 이어서 실행 중 하나를 고른다.
  재개 가능 여부는 LangGraph가 아니라 Orbit의 `run`·`approval` 상태로 판단한다(ADR-0003 조건 4).
"""

import uuid
from typing import Any, Literal

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from orbit.records.approvals import expire_approval
from orbit.records.runs import TERMINAL_RUN_STATUSES, get_run
from orbit.runflow.deps import RunDeps
from orbit.runflow.nodes import RunNodes
from orbit.runflow.repo import latest_approval_for_run, lock_run
from orbit.runflow.state import RunState, validate_state

Outcome = Literal["missing", "terminal", "waiting", "advanced"]


def _route(state: RunState) -> str:
    return state.get("next_node", "done")


def build_graph(deps: RunDeps, checkpointer: Any) -> Any:
    nodes = RunNodes(deps)
    graph = StateGraph(RunState)
    graph.add_node("agent", nodes.agent)
    graph.add_node("await_approval", nodes.await_approval)
    graph.add_node("execute", nodes.execute)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        _route,
        {"agent": "agent", "await_approval": "await_approval", "execute": "execute", "done": END},
    )
    graph.add_edge("await_approval", "execute")
    graph.add_conditional_edges("execute", _route, {"agent": "agent", "done": END})
    return graph.compile(checkpointer=checkpointer)


async def advance_run(deps: RunDeps, workspace_id: uuid.UUID, run_id: uuid.UUID) -> Outcome:
    async with deps.db.workspace_session(workspace_id) as s:
        run = await get_run(s, workspace_id=workspace_id, run_id=run_id)
    if run is None:
        return "missing"
    if run.status in TERMINAL_RUN_STATUSES:
        return "terminal"
    if run.status == "waiting_approval" and not await _approval_decided(deps, workspace_id, run_id):
        return "waiting"

    config = {
        "configurable": {"thread_id": str(run_id)},
        "recursion_limit": deps.max_steps * 3 + 10,
    }
    async with AsyncPostgresSaver.from_conn_string(deps.checkpoint_conninfo) as saver:
        graph = build_graph(deps, saver)
        snapshot = await graph.aget_state(config)
        if not snapshot.values and not snapshot.next:
            initial = validate_state(
                {
                    "run_id": str(run_id),
                    "workspace_id": str(workspace_id),
                    "step": 0,
                    "tool_call_id": "",
                    "next_node": "agent",
                }
            )
            await graph.ainvoke(initial, config, durability="sync")
        elif any(task.interrupts for task in snapshot.tasks):
            await graph.ainvoke(Command(resume=True), config, durability="sync")
        elif snapshot.next:
            await graph.ainvoke(None, config, durability="sync")
    return "advanced"


async def _approval_decided(deps: RunDeps, workspace_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """승인이 결정됐거나 만료됐는가. 만료 시각이 지난 `pending`은 `expired`로 바꾸고 True."""
    async with deps.db.workspace_session(workspace_id) as s:
        if await lock_run(s, workspace_id, run_id) is None:
            return True  # 삭제됨. 아래 진행에서 다시 확인한다.
        approval = await latest_approval_for_run(s, workspace_id, run_id)
        if approval is None:
            return True  # 승인 행이 없는데 waiting이면 노드가 바로잡는다
        if approval.status != "pending":
            return True
        if deps.clock.now() >= approval.expires_at:
            return await expire_approval(
                s, workspace_id=workspace_id, approval_id=approval.id, clock=deps.clock
            )
        return False
