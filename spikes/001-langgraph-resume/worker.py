"""SPIKE-001 워커 CLI. 명령 하나가 독립 프로세스 하나다. 마지막 줄에 `RESULT <json>`을 남긴다."""

import argparse
import json
import sys

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from graph import DSN, build_graph, ensure_effects_table


def pending_of(graph, config) -> list:
    snap = graph.get_state(config)
    return [i.value for t in snap.tasks for i in t.interrupts]


def state_of(graph, config) -> dict:
    snap = graph.get_state(config)
    return {"values": snap.values, "next": list(snap.next), "pending": pending_of(graph, config)}


def run(args) -> dict:
    ensure_effects_table()
    config = {
        "configurable": {
            "thread_id": args.thread_id,
            "plan_seconds": getattr(args, "plan_seconds", 0),
            "execute_seconds": getattr(args, "execute_seconds", 0),
        }
    }
    with PostgresSaver.from_conn_string(DSN) as cp:
        cp.setup()
        graph = build_graph(cp)
        cmd = args.cmd
        if cmd == "start":
            graph.invoke({"request": "start"}, config)
        elif cmd == "resume":
            graph.invoke(Command(resume={"approved": bool(args.approve)}), config)
        elif cmd == "continue":
            graph.invoke(None, config)
        st = state_of(graph, config)
        if cmd == "pending":
            return {"pending": st["pending"]}
        return st


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("thread_id")
    s.add_argument("--plan-seconds", type=float, default=0)
    s = sub.add_parser("pending")
    s.add_argument("thread_id")
    s = sub.add_parser("resume")
    s.add_argument("thread_id")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    s.add_argument("--execute-seconds", type=float, default=0)
    s = sub.add_parser("continue")
    s.add_argument("thread_id")
    s.add_argument("--execute-seconds", type=float, default=0)
    s = sub.add_parser("status")
    s.add_argument("thread_id")
    args = p.parse_args()
    try:
        out = run(args)
    except Exception as e:  # noqa: BLE001
        print("RESULT " + json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
        return 1
    print("RESULT " + json.dumps(out, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
