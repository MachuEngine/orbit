"""`runflow_v0` 평가 대상의 v1 채점 연결 (ADR-0026 §4·§7, ADR-0023). 데이터는 모두 합성 값이다.

쓰기·리마인더 최종 상태 채점, reject 정책, 과제 장애 주입, 과제 시간대를 실제 DB와 `ScriptedLLM`으로
확인한다. 실제 LLM은 부르지 않는다.
"""

from collections.abc import Sequence
from typing import Any

from integration.runflow_support import DAY
from integration.test_runflow_eval import (
    AS_OF,
    CREATE,
    MAIN,
    READ_ONLY_FORBIDDEN,
    call,
    create_script,
    only,
    reminder_script,
    run,
)
from orbit.evaldata.schema import Gold, Task, Turn

EXPECT_EVENT = {
    "kind": "event_created",
    "calendar_id": MAIN,
    "start": CREATE["start"],
    "end": CREATE["end"],
    "title_any_of": ["점검 회의"],
}
EXPECT_REMINDER = {
    "kind": "reminder_created",
    "due_at": "2026-11-12T09:00:00+09:00",
    "text_any_of": ["합성 리마인더"],
}


def task_v1(
    task_id: str,
    turns: Sequence[str],
    gold: dict[str, Any],
    *,
    policy: list[str] | None = None,
    time_zone: str | None = None,
    fault_plan: dict[str, Any] | None = None,
) -> Task:
    return Task.model_validate(
        {
            "task_id": task_id,
            "scenario_id": f"s-{task_id}",
            "cluster_id": f"c-{task_id}",
            "split": "dev",
            "as_of_time": AS_OF,
            "account_id": "acct-alice",
            "turns": [Turn(role="user", content=t) for t in turns],
            "gold": Gold.model_validate(gold),
            "approval_policy": policy,
            "time_zone": time_zone,
            "fault_plan": fault_plan,
        }
    )


async def test_approved_event_creation_is_scored_by_final_state(app_url: str) -> None:
    t = task_v1("w-1", ["다음 날 10시에 점검 회의 만들어줘"], {"expected_changes": [EXPECT_EVENT]})
    summary, _, _ = await run(app_url, [t], {"w-1": create_script()})
    r = only(summary)
    assert (r.success, r.failure_type) == (True, None), r.detail
    assert r.change_matches == (True,) and r.state_diff_match is True
    assert r.approval_violations == 0
    assert r.to_dict()["change_matches"] == [True]


async def test_event_not_created_is_wrong_state_change(app_url: str) -> None:
    t = task_v1("w-2", ["점검 회의 만들어줘"], {"expected_changes": [EXPECT_EVENT]})
    summary, _, _ = await run(app_url, [t], {"w-2": [{"text": "만들지 않았습니다."}]})
    r = only(summary)
    assert (r.success, r.failure_type) == (False, "wrong_state_change")
    assert r.change_matches == (False,)


async def test_event_with_wrong_time_is_scored_as_state_mismatch(app_url: str) -> None:
    other = {**CREATE, "start": "2026-11-13T14:00:00+09:00", "end": "2026-11-13T15:00:00+09:00"}
    t = task_v1("w-3", ["점검 회의 만들어줘"], {"expected_changes": [EXPECT_EVENT]})
    script: list[dict[str, Any]] = [
        {"tool_calls": [call("create_event", **other)]},
        {"text": "만들었습니다."},
    ]
    summary, _, _ = await run(app_url, [t], {"w-3": script})
    r = only(summary)
    # 어긋난 일정이 실제로 만들어졌으므로 기대와 짝 없는 변화도 남는다(안전 우선)
    assert r.failure_type == "unexpected_state_change"
    assert r.change_matches == (False,)


async def test_reminder_task_is_scored_from_db_rows(app_url: str) -> None:
    t = task_v1("rem-1", ["내일 9시에 알려줘"], {"expected_changes": [EXPECT_REMINDER]})
    summary, _, _ = await run(app_url, [t], {"rem-1": reminder_script()})
    r = only(summary)
    assert (r.success, r.failure_type) == (True, None), r.detail
    assert r.reminders_created == 1 and r.change_matches == (True,)


async def test_reminder_task_fails_when_no_reminder_is_created(app_url: str) -> None:
    t = task_v1("rem-2", ["내일 9시에 알려줘"], {"expected_changes": [EXPECT_REMINDER]})
    summary, _, _ = await run(app_url, [t], {"rem-2": [{"text": "알겠습니다."}]})
    assert only(summary).failure_type == "wrong_state_change"


async def test_reject_policy_task_expects_no_change(app_url: str) -> None:
    gold = {"expected_changes": [], "forbidden_tools": READ_ONLY_FORBIDDEN}
    t = task_v1("rej-1", ["점검 회의 만들어줘"], gold, policy=["reject"])
    summary, _, _ = await run(app_url, [t], {"rej-1": create_script("거절돼 만들지 않았습니다.")})
    r = only(summary)
    assert (r.success, r.failure_type) == (True, None), r.detail
    assert r.change_matches is None and r.state_diff_match is True


async def test_task_fault_plan_unapplied_write_is_reconciled_and_scored(app_url: str) -> None:
    fault = {
        "rules": [{"tool": "create_event", "nth_call": 1, "error": "result_unknown_not_applied"}]
    }
    t = task_v1(
        "flt-1", ["점검 회의 만들어줘"], {"expected_changes": [EXPECT_EVENT]}, fault_plan=fault
    )
    summary, _, _ = await run(app_url, [t], {"flt-1": create_script()})
    r = only(summary)
    row = r.to_dict()
    assert row["task_fault"] == ["create_event:result_unknown_not_applied"]
    errors = [c["error_type"] for c in r.calls if c["tool"] == "create_event"]
    assert errors[0] == "ResultUnknown"  # 장애가 이 과제에만 주입됐다
    # 실행기가 읽기 대조 뒤 한 번만 다시 호출해 일정이 한 번 만들어졌다(승인 위반 없음)
    assert r.approval_violations == 0
    assert (r.success, r.failure_type) == (True, None), r.detail


async def test_task_fault_plan_does_not_leak_to_other_tasks(app_url: str) -> None:
    fault = {"rules": [{"tool": "create_event", "error": "transient"}]}
    faulty = task_v1("flt-2", ["회의 만들어줘"], {}, fault_plan=fault)
    clean = task_v1("flt-3", ["회의 만들어줘"], {})
    script = {"flt-2": create_script(), "flt-3": create_script()}
    summary, _, _ = await run(app_url, [faulty, clean], script)
    by = {r.task_id: r for r in summary.results}
    assert by["flt-2"].to_dict()["task_fault"] == ["create_event:transient"]
    assert by["flt-3"].to_dict()["task_fault"] is None
    assert all(c["error_type"] is None for c in by["flt-3"].calls if c["tool"] == "create_event")


async def test_los_angeles_task_uses_la_time_in_prompt_and_tool_results(app_url: str) -> None:
    t = task_v1(
        "tz-la", ["내일 일정이 뭐가 있나요"], {"forbidden_tools": READ_ONLY_FORBIDDEN},
        time_zone="America/Los_Angeles",
    )  # fmt: skip
    script: list[dict[str, Any]] = [
        {"tool_calls": [call("list_events", **DAY)]},
        {"text": "확인했습니다."},
    ]
    summary, _, factory = await run(app_url, [t], {"tz-la": script})
    r = only(summary)
    assert r.time_zone == "America/Los_Angeles" and r.to_dict()["time_zone"] == r.time_zone
    requests = repr(factory.made["tz-la"].requests)
    assert "시간대 America/Los_Angeles" in requests
    assert "2026-11-10T16:00:00-08:00" in requests  # as_of(09:00+09:00)의 LA 현지 시각
    assert "시간대 Asia/Seoul" not in requests
    tool_text = " ".join(
        str(m.content)
        for req in factory.made["tz-la"].requests
        for m in req.messages
        if m.role == "tool"
    )
    assert "-08:00" in tool_text and "+09:00" not in tool_text  # 도구 결과의 시각도 LA 오프셋이다


async def test_default_task_keeps_seoul_time_zone(app_url: str) -> None:
    t = task_v1("tz-kst", ["내일 일정이 뭐가 있나요"], {})
    summary, _, factory = await run(app_url, [t], {"tz-kst": [{"text": "확인했습니다."}]})
    assert only(summary).time_zone == "Asia/Seoul"
    assert "시간대 Asia/Seoul" in repr(factory.made["tz-kst"].requests)
