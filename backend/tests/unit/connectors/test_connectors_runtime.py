import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orbit.connectors.contracts import (
    CALENDAR_TOOLS,
    MAIL_TOOLS,
    AlreadyExists,
    Attendee,
    ConnectorError,
    EventDraft,
    EventPatch,
    NotFound,
    PermissionRevoked,
    PreconditionFailed,
    RateLimited,
    ResultUnknown,
    TransientError,
)
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock


def plan(*rules: dict[str, Any]) -> FaultPlan:
    return FaultPlan.from_dict({"rules": list(rules)})


class Counter:
    """적용 여부를 세는 가짜 상태."""

    def __init__(self) -> None:
        self.applied = 0

    def apply(self) -> str:
        self.applied += 1
        return "done"

    async def apply_async(self) -> str:
        self.applied += 1
        return "done-async"


async def test_no_faults_runs_and_records() -> None:
    rt = SimRuntime()
    c = Counter()
    assert await rt.run_tool("get_event", {"event_id": "e1"}, c.apply) == "done"
    assert await rt.run_tool("get_event", {"event_id": "e2"}, c.apply_async) == "done-async"
    recs = rt.calls.records
    assert [r.seq for r in recs] == [1, 2]
    assert [r.args["event_id"] for r in recs] == ["e1", "e2"]
    assert all(r.outcome == "ok" and r.error_type is None for r in recs)
    json.dumps([r.model_dump(mode="json") for r in recs])  # JSON 직렬화 가능


async def test_nth_call_counts_per_tool() -> None:
    rt = SimRuntime(plan({"tool": "create_event", "nth_call": 2, "error": "transient"}))
    c = Counter()
    await rt.run_tool("get_event", {}, c.apply)  # 다른 도구는 세지 않는다
    await rt.run_tool("create_event", {}, c.apply)
    with pytest.raises(TransientError):
        await rt.run_tool("create_event", {}, c.apply)
    await rt.run_tool("create_event", {}, c.apply)  # 3번째는 정상
    assert c.applied == 3
    assert [r.outcome for r in rt.calls.records] == ["ok", "ok", "error", "ok"]
    assert rt.calls.records[2].error_type == "TransientError"


async def test_match_is_partial_and_nth_counts_matching_calls() -> None:
    rt = SimRuntime(
        plan(
            {
                "tool": "update_event",
                "match": {"calendar_id": "c1", "patch": {"title": "x"}},
                "nth_call": 2,
                "error": "precondition_failed",
            }
        )
    )
    c = Counter()
    other = {"calendar_id": "c2", "event_id": "e", "patch": {"title": "x"}}
    hit = {"calendar_id": "c1", "event_id": "e", "patch": {"title": "x", "location": "y"}}
    miss = {"calendar_id": "c1", "event_id": "e", "patch": {"title": "z"}}
    await rt.run_tool("update_event", other, c.apply)
    await rt.run_tool("update_event", hit, c.apply)  # 일치 1번째
    await rt.run_tool("update_event", miss, c.apply)
    with pytest.raises(PreconditionFailed):
        await rt.run_tool("update_event", hit, c.apply)  # 일치 2번째
    assert c.applied == 3


async def test_match_against_model_args() -> None:
    rt = SimRuntime(
        plan({"tool": "create_event", "match": {"draft": {"title": "A"}}, "error": "not_found"})
    )
    from datetime import UTC, datetime

    t = datetime(2026, 11, 11, tzinfo=UTC)
    with pytest.raises(NotFound):
        await rt.run_tool(
            "create_event", {"draft": EventDraft(title="A", start=t, end=t)}, lambda: 1
        )
    assert (
        await rt.run_tool(
            "create_event", {"draft": EventDraft(title="B", start=t, end=t)}, lambda: 1
        )
        == 1
    )


async def test_rule_without_nth_applies_every_call() -> None:
    rt = SimRuntime(plan({"tool": "t", "error": "permission_revoked"}))
    for _ in range(2):
        with pytest.raises(PermissionRevoked):
            await rt.run_tool("t", {}, lambda: 1)


@pytest.mark.parametrize(
    ("error", "exc"),
    [
        ("not_found", NotFound),
        ("already_exists", AlreadyExists),
        ("precondition_failed", PreconditionFailed),
        ("rate_limited", RateLimited),
        ("permission_revoked", PermissionRevoked),
        ("transient", TransientError),
        ("result_unknown_not_applied", ResultUnknown),
    ],
)
async def test_pre_apply_errors_do_not_apply(error: str, exc: type[ConnectorError]) -> None:
    rt = SimRuntime(plan({"tool": "t", "error": error}))
    c = Counter()
    with pytest.raises(exc):
        await rt.run_tool("t", {}, c.apply)
    assert c.applied == 0


async def test_rate_limited_carries_retry_after() -> None:
    rt = SimRuntime(plan({"tool": "t", "error": "rate_limited", "retry_after_s": 1.5}))
    with pytest.raises(RateLimited) as info:
        await rt.run_tool("t", {}, lambda: 1)
    assert info.value.retry_after_s == 1.5


async def test_result_unknown_applies_but_not_applied_variant_does_not() -> None:
    c1 = Counter()
    rt1 = SimRuntime(plan({"tool": "t", "error": "result_unknown"}))
    with pytest.raises(ResultUnknown):
        await rt1.run_tool("t", {}, c1.apply)
    assert c1.applied == 1  # 적용 후 응답 유실

    c2 = Counter()
    rt2 = SimRuntime(plan({"tool": "t", "error": "result_unknown_not_applied"}))
    with pytest.raises(ResultUnknown):
        await rt2.run_tool("t", {}, c2.apply)
    assert c2.applied == 0

    for rt in (rt1, rt2):
        (rec,) = rt.calls.records
        assert rec.outcome == "error"
        assert rec.error_type == "ResultUnknown"


async def test_apply_error_is_recorded_and_reraised() -> None:
    rt = SimRuntime()

    def boom() -> None:
        raise NotFound("없다")

    with pytest.raises(NotFound):
        await rt.run_tool("get_event", {"event_id": "x"}, boom)
    (rec,) = rt.calls.records
    assert (rec.outcome, rec.error_type) == ("error", "NotFound")


async def test_call_record_does_not_copy_bodies() -> None:
    rt = SimRuntime()
    body = "비밀 아닌 합성 본문 " * 20
    await rt.run_tool("create_draft", {"spec": {"subject": "s", "body_text": body}}, lambda: None)
    (rec,) = rt.calls.records
    assert body not in json.dumps(rec.model_dump(mode="json"), ensure_ascii=False)
    assert rec.args["spec"]["body_text"].startswith("<str len=")


def test_fault_plan_rejects_unknown_error_and_fields() -> None:
    with pytest.raises(ValidationError):
        plan({"tool": "t", "error": "boom"})
    with pytest.raises(ValidationError):
        plan({"tool": "t", "error": "transient", "nth": 1})
    with pytest.raises(ValidationError):
        plan({"tool": "t", "error": "transient", "nth_call": 0})


async def test_match_normalizes_datetime_and_nested_models() -> None:
    start = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)
    draft = EventDraft(title="A", start=start, end=datetime(2026, 11, 12, 2, 0, tzinfo=UTC))
    # match에 datetime 객체를 써도 JSON 형태로 정규화돼 일치한다.
    rt = SimRuntime(
        plan({"tool": "create_event", "match": {"draft": {"start": start}}, "error": "not_found"})
    )
    with pytest.raises(NotFound):
        await rt.run_tool("create_event", {"draft": draft}, lambda: "ok")
    # 문자열은 pydantic JSON 표기(UTC는 Z)와 같아야 한다. 인자가 datetime이어도 일치한다.
    rt2 = SimRuntime(
        plan(
            {
                "tool": "list_events",
                "match": {"time_min": "2026-11-12T01:00:00Z"},
                "error": "not_found",
            }
        )
    )
    with pytest.raises(NotFound):
        await rt2.run_tool("list_events", {"time_min": start}, lambda: "ok")
    # 튜플 값은 리스트로 정규화된다.
    patch = EventPatch(attendees=(Attendee(email="bob@example.org"),))
    rt3 = SimRuntime(
        plan(
            {
                "tool": "update_event",
                "match": {
                    "patch": {
                        "attendees": [
                            {
                                "email": "bob@example.org",
                                "name": None,
                                "response_status": "needs_action",
                            }
                        ]
                    }
                },
                "error": "not_found",
            }
        )
    )
    with pytest.raises(NotFound):
        await rt3.run_tool("update_event", {"patch": patch}, lambda: "ok")
    # 다른 시각이면 일치하지 않는다.
    other = SimRuntime(
        plan({"tool": "list_events", "match": {"time_min": start}, "error": "not_found"})
    )
    later = datetime(2026, 11, 12, 2, 0, tzinfo=UTC)
    assert await other.run_tool("list_events", {"time_min": later}, lambda: "ok") == "ok"


def test_bind_rejects_unknown_tool_for_dedicated_runtime() -> None:
    rt = SimRuntime(plan({"tool": "create_evnt", "error": "transient"}))
    with pytest.raises(ValueError, match="create_evnt"):
        rt.bind(CALENDAR_TOOLS)
    # 다른 커넥터의 도구도 전용 런타임에서는 거절한다(조용히 무시되는 규칙 방지).
    with pytest.raises(ValueError, match="send_draft"):
        SimRuntime(plan({"tool": "send_draft", "error": "transient"})).bind(CALENDAR_TOOLS)


def test_shared_runtime_binds_both_and_seal_finds_orphans() -> None:
    rt = SimRuntime(
        plan(
            {"tool": "create_event", "error": "transient"},
            {"tool": "send_draft", "error": "transient"},
        ),
        shared=True,
    )
    rt.bind(CALENDAR_TOOLS)  # 메일 규칙이 있어도 바인딩 순서와 무관하게 통과한다
    with pytest.raises(ValueError, match="send_draft"):
        rt.seal()
    assert not rt.sealed
    rt.bind(MAIL_TOOLS)
    rt.seal()
    assert rt.sealed
    with pytest.raises(RuntimeError, match="봉인"):
        rt.bind(MAIL_TOOLS)
    with pytest.raises(ValueError, match="oops"):
        SimRuntime(plan({"tool": "oops", "error": "transient"}), shared=True).bind(CALENDAR_TOOLS)


async def test_shared_runtime_rejects_run_tool_before_seal() -> None:
    rt = SimRuntime(shared=True)
    rt.bind(CALENDAR_TOOLS)
    with pytest.raises(RuntimeError, match="seal"):
        await rt.run_tool("get_event", {}, lambda: "ok")
    rt.seal()
    assert await rt.run_tool("get_event", {}, lambda: "ok") == "ok"
    # 전용 런타임은 바인딩 즉시(봉인 없이) 쓸 수 있다.
    assert await SimRuntime().run_tool("get_event", {}, lambda: "ok") == "ok"


async def test_match_converts_offsets_to_utc() -> None:
    kst = timezone(timedelta(hours=9))
    rt = SimRuntime(
        plan(
            {
                "tool": "list_events",
                "match": {"time_min": datetime(2026, 11, 12, 10, 0, tzinfo=kst)},
                "error": "not_found",
            }
        )
    )
    with pytest.raises(NotFound):
        await rt.run_tool(
            "list_events", {"time_min": datetime(2026, 11, 12, 1, 0, tzinfo=UTC)}, lambda: "ok"
        )


def _world() -> SimWorld:
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "sim" / "minimal_world.json"
    return SimWorld.load(fixture, FixedClock(datetime(2026, 11, 11, 9, 0, tzinfo=UTC)))


def test_factory_rejects_rule_belonging_to_no_connector() -> None:
    with pytest.raises(ValueError, match="get_evnt"):
        build_sim_connectors(
            _world(), ["acct-alice"], plan({"tool": "get_evnt", "error": "transient"})
        )
    # 계정이 하나도 없으면 어떤 규칙도 적용될 수 없다.
    with pytest.raises(ValueError, match="get_event"):
        build_sim_connectors(_world(), [], plan({"tool": "get_event", "error": "transient"}))


async def test_factory_injects_faults_per_account_across_mail_and_calendar() -> None:
    """4단계 대비: 한 FaultPlan이 메일·캘린더 도구를 함께 담고 account_id로 계정을 구분한다."""
    conns = build_sim_connectors(
        _world(),
        ["acct-alice", "acct-bob"],
        plan(
            {"tool": "get_message", "match": {"account_id": "acct-bob"}, "error": "transient"},
            {"tool": "get_event", "match": {"account_id": "acct-bob"}, "error": "transient"},
        ),
    )
    assert conns.runtime.sealed
    assert (await conns.mail["acct-alice"].get_message("msg-a-001")).id == "msg-a-001"
    with pytest.raises(TransientError):
        await conns.mail["acct-bob"].get_message("msg-b-001")
    with pytest.raises(TransientError):
        await conns.calendar["acct-bob"].get_event("cal-bob-main", "evbob00001")
    cal = conns.calendar["acct-alice"]
    assert (await cal.get_event("cal-alice-main", "evalice001")).id == "evalice001"
    assert [r.tool for r in conns.calls.records] == [
        "get_message",
        "get_message",
        "get_event",
        "get_event",
    ]
