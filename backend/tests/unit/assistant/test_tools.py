import json
import re
from typing import Any

import pytest

from orbit.assistant.tools import (
    MAX_EXTERNAL_CHARS,
    ReadOnlyTools,
    tool_specs,
    wrap_external,
)
from orbit.assistant.workset import UnknownHandleError, WorkSet

from .conftest import DAY, INJECTION, provider_ids


async def call(tools: ReadOnlyTools, name: str, **args: Any) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((await tools.execute(name, args)).content)
    return result


def test_workset_same_target_same_handle_and_first_seen_order() -> None:
    ws = WorkSet()
    assert ws.event_handle("c1", "e1") == "E1"
    assert ws.event_handle("c1", "e2") == "E2"
    assert ws.event_handle("c1", "e1") == "E1"
    assert ws.event_handle("c2", "e1") == "E3"  # 캘린더가 다르면 다른 대상
    assert ws.message_handle("m-x") == "M1"
    assert ws.message_handle("m-x") == "M1"
    assert ws.resolve_event("E2") == ("c1", "e2")
    assert ws.resolve_event(" e2 ") == ("c1", "e2")
    assert ws.resolve_message("M1") == "m-x"


def test_workset_unknown_handles_raise() -> None:
    ws = WorkSet()
    ws.event_handle("c1", "e1")
    ws.message_handle("m1")
    for bad in ("E2", "M1", "E01", "e1x", "", None, 3, "m1"):
        try:
            ws.resolve_event(bad)
        except UnknownHandleError:
            continue
        raise AssertionError(f"event {bad!r}")
    for bad in ("M2", "E1", None):
        try:
            ws.resolve_message(bad)
        except UnknownHandleError:
            continue
        raise AssertionError(f"message {bad!r}")


async def test_list_events_merges_calendars_sorted_with_handles(tools: ReadOnlyTools) -> None:
    out = await call(
        tools, "list_events", time_min="2026-11-12T00:00:00+09:00", time_max="2026-11-14T00:00:00Z"
    )
    events = out["events"]
    assert [e["title"] for e in events] == ["주간 회의", "점심", "긴 설명 일정", "팀 회고"]
    assert [e["handle"] for e in events] == ["E1", "E2", "E3", "E4"]
    first = events[0]
    assert first["start"] == "2026-11-12T10:00:00+09:00"  # 사용자 시간대 표기
    assert first["end"] == "2026-11-12T11:00:00+09:00"
    assert first["all_day"] is False and first["location"] == "회의실 A"
    assert first["attendees"] == [{"name": "밥", "email": "bob@example.org"}]
    assert first["organizer"] == {"name": "앨리스", "email": "alice@example.com"}
    assert first["status"] == "confirmed"
    assert set(first) == {
        "handle",
        "title",
        "start",
        "end",
        "all_day",
        "location",
        "attendees",
        "organizer",
        "status",
    }


async def test_cancelled_events_are_excluded(tools: ReadOnlyTools) -> None:
    out = await call(tools, "list_events", **DAY)
    assert "취소된 회의" not in [e["title"] for e in out["events"]]


async def test_list_events_query_filters(tools: ReadOnlyTools) -> None:
    out = await call(tools, "list_events", **DAY, query="점심")
    assert [e["title"] for e in out["events"]] == ["점심"]


async def test_handles_are_stable_across_calls(tools: ReadOnlyTools) -> None:
    a = await call(tools, "list_events", **DAY)
    b = await call(tools, "list_events", **DAY)
    assert a == b


async def test_no_provider_ids_in_any_result(tools: ReadOnlyTools) -> None:
    results = [
        await call(tools, "list_events", **DAY),
        await call(tools, "get_event", handle="E1"),
        await call(tools, "search_mail", query="from:bob@example.org"),
        await call(tools, "read_mail", handle="M1"),
    ]
    text = json.dumps(results, ensure_ascii=False)
    for pid in provider_ids():
        assert pid not in text


async def test_get_event_wraps_and_truncates_description(tools: ReadOnlyTools) -> None:
    await call(tools, "list_events", **DAY)
    out = await call(tools, "get_event", handle="E3")
    desc = out["description"]
    assert desc.startswith("<external_content>") and desc.endswith("</external_content>")
    assert out["description_truncated"] is True
    inner = desc[len("<external_content>") : -len("</external_content>")]
    assert len(inner) <= MAX_EXTERNAL_CHARS + 10  # 이스케이프로 약간 늘 수 있다
    assert "</external_content>" not in inner  # 본문이 표지를 닫지 못한다
    assert INJECTION.split(" ")[0] in inner  # 내용은 데이터로 그대로 전달


async def test_get_event_without_description_is_wrapped_and_not_truncated(
    tools: ReadOnlyTools,
) -> None:
    await call(tools, "list_events", **DAY)
    out = await call(tools, "get_event", handle="E2")
    assert out["description"] == "<external_content></external_content>"
    assert out["description_truncated"] is False


def test_closing_marker_variants_are_neutralized() -> None:
    for variant in (
        "</external_content>",
        "</ EXTERNAL_content >",
        "< / External_Content>",
        "<EXTERNAL_CONTENT",
    ):
        wrapped, _ = wrap_external(f"앞 {variant} 뒤")
        inner = wrapped[len("<external_content>") : -len("</external_content>")]
        assert not re.search(r"<\s*/?\s*external_content", inner, re.IGNORECASE), variant


async def test_short_fields_are_capped(make_connectors: Any) -> None:
    from orbit.connectors.sim.factory import build_sim_connectors
    from orbit.connectors.sim.world import SimWorld
    from orbit.core.clock import FixedClock

    from .conftest import ACCOUNT, NOW, TZ, world_snapshot

    data = world_snapshot()
    acct = data["accounts"][ACCOUNT]
    acct["events"][0].update(title="제" * 500, location="장" * 500)
    acct["events"][0]["organizer"]["name"] = "주" * 500
    acct["events"][0]["attendees"][0]["name"] = "참" * 500
    acct["messages"][0].update(subject="목" * 500)
    acct["messages"][0]["from"]["name"] = "발" * 500
    conn = build_sim_connectors(SimWorld.load(data, FixedClock(NOW)), [ACCOUNT])
    t = ReadOnlyTools(calendar=conn.calendar[ACCOUNT], mail=conn.mail[ACCOUNT], tz=TZ)
    ev = (await call(t, "list_events", **DAY))["events"][0]
    assert len(ev["title"]) == len(ev["location"]) == 200
    assert len(ev["organizer"]["name"]) == len(ev["attendees"][0]["name"]) == 200
    found = (await call(t, "search_mail", query="from:bob@example.org"))["messages"]
    assert any(len(m["subject"]) == 200 and len(m["from"]["name"]) == 200 for m in found)


async def test_unexpected_errors_are_not_swallowed(tools: ReadOnlyTools) -> None:
    class Boom:
        async def list_calendars(self) -> Any:
            raise ValueError("버그")

    t = ReadOnlyTools(calendar=Boom(), mail=tools._mail, tz=tools._tz)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await t.execute("list_events", DAY)


def test_wrap_external_limit() -> None:
    wrapped, truncated = wrap_external("a" * MAX_EXTERNAL_CHARS)
    assert not truncated and wrapped.count("a") - 2 == MAX_EXTERNAL_CHARS
    wrapped, truncated = wrap_external("a" * (MAX_EXTERNAL_CHARS + 1))
    assert truncated and wrapped.count("a") - 2 == MAX_EXTERNAL_CHARS


async def test_search_and_read_mail(tools: ReadOnlyTools) -> None:
    found = await call(tools, "search_mail", query="from:bob@example.org")
    assert "프로젝트 일정 문의" in [m["subject"] for m in found["messages"]]
    handle = next(m["handle"] for m in found["messages"] if m["subject"] == "프로젝트 일정 문의")
    mail = await call(tools, "read_mail", handle=handle)
    assert mail["subject"] == "프로젝트 일정 문의"
    assert mail["from"] == {"name": "밥", "email": "bob@example.org"}
    assert mail["to"] == [{"name": "앨리스", "email": "alice@example.com"}]
    assert mail["date"].endswith("+09:00")
    assert mail["body"].startswith("<external_content>")
    assert mail["body_truncated"] is False


async def test_read_long_mail_is_truncated(tools: ReadOnlyTools) -> None:
    found = await call(tools, "search_mail", query="subject:긴")
    handle = found["messages"][0]["handle"]
    mail = await call(tools, "read_mail", handle=handle)
    assert mail["body_truncated"] is True
    assert len(mail["body"]) <= MAX_EXTERNAL_CHARS + 60


async def test_errors_are_returned_to_model_without_raw_messages(tools: ReadOnlyTools) -> None:
    assert await call(tools, "get_event", handle="E9") == {"error": "unknown_handle"}
    assert await call(tools, "read_mail", handle="M9") == {"error": "unknown_handle"}
    assert await call(tools, "read_mail", handle="E1") == {"error": "unknown_handle"}
    naive = await call(
        tools, "list_events", time_min="2026-11-12T00:00:00", time_max="2026-11-13T00:00:00+09:00"
    )
    assert naive == {"error": "invalid_time"}
    junk = await call(tools, "list_events", time_min="내일", time_max="2026-11-13T00:00:00+09:00")
    assert junk == {"error": "invalid_time"}
    reversed_ = await call(tools, "list_events", time_min=DAY["time_max"], time_max=DAY["time_min"])
    assert reversed_ == {"error": "invalid_time_range"}
    assert await call(tools, "list_events", time_min=DAY["time_min"]) == {
        "error": "invalid_arguments"
    }
    assert await call(tools, "search_mail", query="a OR b") == {"error": "invalid_query"}
    assert await call(tools, "search_mail", query="a", max_results=0) == {
        "error": "invalid_arguments"
    }
    assert await call(tools, "search_mail", query="a", max_results=True) == {
        "error": "invalid_arguments"
    }
    assert await call(tools, "send_draft", draft_id="x") == {"error": "unknown_tool"}


async def test_error_outcome_is_flagged(tools: ReadOnlyTools) -> None:
    outcome = await tools.execute("get_event", {"handle": "E9"})
    assert outcome.is_error and outcome.structured is None


def test_tools_are_read_only() -> None:
    names = {s.name for s in tool_specs()}
    assert names == {"list_events", "get_event", "search_mail", "read_mail", "submit_answer"}
    write_words = ("create", "update", "cancel", "delete", "send", "draft")
    assert not [n for n in names if any(w in n for w in write_words)]


def test_search_tool_description_states_query_subset() -> None:
    spec = next(s for s in tool_specs() if s.name == "search_mail")
    for token in (
        "from:",
        "to:",
        "subject:",
        "after:",
        "before:",
        "has:attachment",
        "in:sent",
        "OR",
    ):
        assert token in spec.description


async def test_submit_answer_converts_all_four_kinds(tools: ReadOnlyTools) -> None:
    await call(tools, "list_events", **DAY)  # E1 주간 회의, E2 점심
    ref1 = {"calendar_id": "cal-alice-main", "event_id": "evalice001"}
    ref2 = {"calendar_id": "cal-alice-main", "event_id": "evalice002"}

    async def submit(**args: Any) -> Any:
        return (await tools.execute("submit_answer", args)).structured

    assert await submit(kind="event_set", events=["E1", "E2", "E1"]) == {
        "kind": "event_set",
        "events": [ref1, ref2],
    }
    assert await submit(kind="event_set", events=[]) == {"kind": "event_set", "events": []}
    assert await submit(kind="event", event="E2") == {"kind": "event", "event": ref2}
    assert await submit(kind="event_time", event="E1", start="2026-11-12T10:30:00+09:00") == {
        "kind": "event_time",
        "event": ref1,
        "start": "2026-11-12T10:30:00+09:00",
    }
    assert await submit(kind="time_slot", start="2026-11-12T14:00:00+09:00") == {
        "kind": "time_slot",
        "start": "2026-11-12T14:00:00+09:00",
    }


async def test_submit_answer_errors_do_not_count_as_submission(tools: ReadOnlyTools) -> None:
    await call(tools, "list_events", **DAY)
    cases: list[tuple[dict[str, Any], str]] = [
        ({"kind": "event_set", "events": ["E1", "E99"]}, "unknown_handle"),
        ({"kind": "event", "event": "M1"}, "unknown_handle"),
        ({"kind": "event"}, "unknown_handle"),
        ({"kind": "event_time", "event": "E1", "start": "2026-11-12T10:30:00"}, "invalid_time"),
        ({"kind": "time_slot"}, "invalid_time"),
        ({"kind": "event_set"}, "invalid_arguments"),
        ({"kind": "other"}, "invalid_kind"),
        ({}, "invalid_kind"),
    ]
    for args, kind in cases:
        outcome = await tools.execute("submit_answer", args)
        assert outcome.structured is None and outcome.is_error, args
        assert json.loads(outcome.content) == {"error": kind}, args
