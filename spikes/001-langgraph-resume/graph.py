"""SPIKE-001 실험 그래프: plan -> approval -> execute | rejected. 스파이크 코드이며 제품 코드가 아니다."""

import hashlib
import json
import os
import time
from typing import Any, TypedDict

import psycopg
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

PG_PASSWORD = os.environ.get("ORBIT_SPIKE_PG_PASSWORD", "spike")
DSN = f"postgresql://orbit_spike:{PG_PASSWORD}@localhost:55432/orbit_spike"

EFFECTS_DDL = """
CREATE TABLE IF NOT EXISTS spike_effects (
    id bigserial PRIMARY KEY,
    thread_id text,
    kind text,
    pid int,
    at timestamptz DEFAULT now()
)
"""

TOOL_ARGS = {"event_id": "evt-1", "start": "2026-01-01T10:00:00Z", "title": "synthetic meeting"}


class State(TypedDict, total=False):
    request: str
    decision: str
    result: str


def marker(name: str) -> None:
    print(f"MARKER {name}", flush=True)


def record_effect(thread_id: str, kind: str) -> None:
    """체크포인트와 무관한 별도 연결로 즉시 커밋한다 (실제 외부 쓰기를 흉내 낸다)."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO spike_effects (thread_id, kind, pid) VALUES (%s, %s, %s)",
            (thread_id, kind, os.getpid()),
        )


def args_hash(tool: str, args: dict[str, Any]) -> str:
    canonical = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _cfg(config: dict, key: str) -> float:
    return float(config.get("configurable", {}).get(key, 0))


def plan(state: State, config) -> dict:
    marker("plan_started")
    time.sleep(_cfg(config, "plan_seconds"))
    return {"request": "update calendar event"}


def approval(state: State, config) -> dict:
    thread_id = config["configurable"]["thread_id"]
    record_effect(thread_id, "pre_interrupt")
    tool = "calendar.update_event"
    answer = interrupt({"tool": tool, "args": TOOL_ARGS, "args_hash": args_hash(tool, TOOL_ARGS)})
    return {"decision": "approved" if answer.get("approved") else "rejected"}


def route(state: State) -> str:
    return "execute" if state.get("decision") == "approved" else "rejected"


def execute(state: State, config) -> dict:
    thread_id = config["configurable"]["thread_id"]
    record_effect(thread_id, "external_write")
    marker("external_write_committed")
    time.sleep(_cfg(config, "execute_seconds"))
    return {"result": "executed"}


def rejected(state: State) -> dict:
    return {"result": "rejected"}


def build_graph(checkpointer):
    g = StateGraph(State)
    g.add_node("plan", plan)
    g.add_node("approval", approval)
    g.add_node("execute", execute)
    g.add_node("rejected", rejected)
    g.add_edge(START, "plan")
    g.add_edge("plan", "approval")
    g.add_conditional_edges("approval", route, {"execute": "execute", "rejected": "rejected"})
    g.add_edge("execute", END)
    g.add_edge("rejected", END)
    return g.compile(checkpointer=checkpointer)


def ensure_effects_table() -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(EFFECTS_DDL)

