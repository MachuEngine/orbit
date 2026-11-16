"""쓰기·리마인더 최종 상태 채점 (ADR-0026 §4). 모든 데이터는 합성 값이다."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import TypeAdapter

from orbit.connectors.sim.calls import CallRecord
from orbit.connectors.sim.world import (
    DraftCreated,
    DraftDeleted,
    DraftSent,
    EventCancelled,
    EventCreated,
    EventUpdated,
    FieldChange,
    MessageAdded,
    WorldDiff,
)
from orbit.evaldata.schema import ExpectedChange, Gold
from orbit.evalrunner.runflow_env import ReminderCreated
from orbit.evalrunner.scoring import score_episode

ADAPTER: TypeAdapter[ExpectedChange] = TypeAdapter(ExpectedChange)
CAL = "cal-1"
START = "2026-11-13T10:00:00+09:00"
END = "2026-11-13T11:00:00+09:00"


def gold(*changes: dict[str, Any], **kw: Any) -> Gold:
    return Gold.model_validate({"expected_changes": list(changes), **kw})


def created_event(**over: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "calendar_id": CAL,
        "title": "합성 점검 회의",
        "description": "",
        "location": "",
        "start": "2026-11-13T01:00:00Z",  # START와 같은 순간(표기만 다르다)
        "end": "2026-11-13T02:00:00Z",
        "all_day": False,
        "attendees": [
            {"email": "Pat@Example.org", "name": None, "response_status": "needs_action"}
        ],
        "organizer": None,
        "status": "confirmed",
    }
    ev.update(over)
    return ev


EVENT_CREATED = {
    "kind": "event_created",
    "calendar_id": CAL,
    "start": START,
    "end": END,
    "title_any_of": ["점검 회의"],
    "attendees_include": ["pat@example.org"],
}


def draft(
    to: str = "kim@example.org", body: str = "견적서를 보냅니다.", **spec: Any
) -> dict[str, Any]:
    return {
        "thread_id": "thr-1",
        "spec": {
            "to": [{"email": to, "name": None}],
            "cc": [],
            "subject": "견적 문의",
            "body_text": body,
            "reply_to_message_id": "m-1",
            "execution_id": None,
            **spec,
        },
    }


def score(g: Gold, diff: WorldDiff, **kw: Any):  # type: ignore[no-untyped-def]
    return score_episode(g, "", [], diff, **kw)


def with_event(**over: Any) -> WorldDiff:
    return WorldDiff(events_created=(EventCreated("a", created_event(**over)),))


# ---------------------------------------------------------------- event_created


def test_event_created_matches_with_instant_comparison_and_case_insensitive_email() -> None:
    s = score(gold(EVENT_CREATED), with_event())
    assert (s.success, s.failure_type, s.state_diff_match) == (True, None, True)
    assert s.change_matches == (True,)
    assert s.checks["state_changes"] is True and s.checks["state_diff"] is True


@pytest.mark.parametrize(
    "over",
    [
        {"calendar_id": "cal-2"},
        {"start": "2026-11-13T02:00:00Z"},
        {"end": "2026-11-13T03:00:00Z"},
        {"all_day": True},
        {"title": "다른 일정"},
        {"attendees": []},
    ],
)
def test_event_created_mismatch_is_wrong_state_change_and_leaves_actual(
    over: dict[str, Any],
) -> None:
    s = score(gold(EVENT_CREATED), with_event(**over))
    # 어긋난 실제 변화는 기대와 짝을 못 찾아 남는 변화이기도 하다 -> 안전 우선으로 unexpected
    assert s.failure_type == "unexpected_state_change"
    assert s.change_matches == (False,)


def test_event_created_without_end_ignores_end_but_checks_other_fields() -> None:
    no_end = {k: v for k, v in EVENT_CREATED.items() if k != "end"}
    assert Gold.model_validate({"expected_changes": [no_end]})
    for over in ({}, {"end": "2026-11-13T05:00:00Z"}):
        assert score(gold(no_end), with_event(**over)).success is True
    assert score(gold(no_end), with_event(start="2026-11-13T02:00:00Z")).success is False


def test_event_created_with_end_still_checks_end() -> None:
    s = score(gold(EVENT_CREATED), with_event(end="2026-11-13T05:00:00Z"))
    assert s.change_matches == (False,)


def test_missing_change_is_wrong_state_change() -> None:
    s = score(gold(EVENT_CREATED), WorldDiff())
    assert (s.success, s.failure_type) == (False, "wrong_state_change")
    assert s.state_diff_match is False and s.change_matches == (False,)


def test_extra_change_is_unexpected_state_change() -> None:
    diff = WorldDiff(
        events_created=(EventCreated("a", created_event()),),
        events_cancelled=(EventCancelled("a", CAL, "ev-9"),),
    )
    s = score(gold(EVENT_CREATED), diff)
    assert s.failure_type == "unexpected_state_change"
    assert s.change_matches == (True,)


def test_unexpected_wins_when_both_missing_and_extra() -> None:
    diff = WorldDiff(events_cancelled=(EventCancelled("a", CAL, "ev-9"),))
    s = score(gold(EVENT_CREATED), diff)
    assert s.failure_type == "unexpected_state_change"
    assert s.change_matches == (False,)


def test_all_day_and_location_conditions() -> None:
    exp = {**EVENT_CREATED, "all_day": True, "location_any_of": ["본관"]}
    exp["start"], exp["end"] = "2026-11-13T00:00:00+09:00", "2026-11-14T00:00:00+09:00"
    ev = created_event(
        all_day=True, start="2026-11-12T15:00:00Z", end="2026-11-13T15:00:00Z", location="본관 3층"
    )
    assert score(gold(exp), WorldDiff(events_created=(EventCreated("a", ev),))).success is True
    ev2 = {**ev, "location": "별관"}
    assert score(gold(exp), WorldDiff(events_created=(EventCreated("a", ev2),))).success is False


def test_two_identical_expectations_need_two_distinct_actual_changes() -> None:
    one = with_event()
    assert score(gold(EVENT_CREATED, EVENT_CREATED), one).failure_type == "wrong_state_change"
    two = WorldDiff(events_created=(EventCreated("a", created_event()),) * 2)
    assert score(gold(EVENT_CREATED, EVENT_CREATED), two).success is True


def test_matching_is_maximum_not_greedy() -> None:
    broad = {**EVENT_CREATED, "attendees_include": []}
    narrow = EVENT_CREATED  # 참석자 필요
    a = created_event()
    b = created_event(attendees=[])
    diff = WorldDiff(events_created=(EventCreated("a", a), EventCreated("a", b)))
    # 넓은 기대가 먼저 오면 탐욕 매칭은 a를 가져가 narrow가 실패한다
    s = score(gold(broad, narrow), diff)
    assert s.success is True and s.change_matches == (True, True)


# ---------------------------------------------------------------- event_updated / cancelled


def change(before: Any, after: Any) -> FieldChange:
    return FieldChange(before, after)


UPDATED = {
    "kind": "event_updated",
    "calendar_id": CAL,
    "event_id": "ev-1",
    "fields": {"start": "2026-11-13T15:00:00+09:00", "end": "2026-11-13T16:00:00+09:00"},
}


def updated(**changes: FieldChange) -> WorldDiff:
    return WorldDiff(events_updated=(EventUpdated("a", CAL, "ev-1", changes),))


MOVE = {
    "start": change("2026-11-13T01:00:00Z", "2026-11-13T06:00:00Z"),
    "end": change("2026-11-13T02:00:00Z", "2026-11-13T07:00:00Z"),
}


def test_event_updated_matches_and_allows_description_change() -> None:
    assert score(gold(UPDATED), updated(**MOVE)).success is True
    with_description = updated(**MOVE, description=change("", "메모"))
    assert score(gold(UPDATED), with_description).success is True


def test_event_updated_core_field_not_expected_is_a_mismatch() -> None:
    s = score(gold(UPDATED), updated(**MOVE, location=change("", "본관")))
    assert s.failure_type == "unexpected_state_change" and s.change_matches == (False,)
    s2 = score(gold(UPDATED), updated(**MOVE, all_day=change(False, True)))
    assert s2.change_matches == (False,)


def test_event_updated_wrong_value_or_unchanged_field_is_a_mismatch() -> None:
    wrong = updated(start=change("2026-11-13T01:00:00Z", "2026-11-13T09:00:00Z"), end=MOVE["end"])
    assert score(gold(UPDATED), wrong).change_matches == (False,)
    assert score(gold(UPDATED), updated(start=MOVE["start"])).change_matches == (False,)


def test_event_updated_title_location_attendees_fields() -> None:
    exp = {
        "kind": "event_updated",
        "calendar_id": CAL,
        "event_id": "ev-1",
        "fields": {
            "title_any_of": ["신규"],
            "location": "본관",
            "attendees_include": ["lee@example.org"],
        },
    }
    ok = updated(
        title=change("옛", "신규 회의"),
        location=change("", "본관 2층"),
        attendees=change([], [{"email": "Lee@example.org", "name": None}]),
    )
    assert score(gold(exp), ok).success is True
    assert score(gold(exp), updated(title=ok.events_updated[0].changes["title"])).success is False


def test_event_cancelled() -> None:
    exp = {"kind": "event_cancelled", "calendar_id": CAL, "event_id": "ev-1"}
    assert score(gold(exp), WorldDiff(events_cancelled=(EventCancelled("a", CAL, "ev-1"),))).success
    other = WorldDiff(events_cancelled=(EventCancelled("a", CAL, "ev-2"),))
    assert score(gold(exp), other).failure_type == "unexpected_state_change"


# ---------------------------------------------------------------- 초안


DRAFT_SENT = {
    "kind": "draft_sent",
    "to_include": ["KIM@example.org"],
    "reply_to_message_id": "m-1",
    "subject_any_of": ["견적"],
    "body_facts": [{"fact_id": "f", "any_of": ["견적서"], "description": "d"}],
    "body_must_not_contain": ["기밀"],
}
DRAFT_CREATED = {**DRAFT_SENT, "kind": "draft_created"}


def test_created_then_sent_draft_is_one_draft_sent_change() -> None:
    d = draft()
    diff = WorldDiff(
        drafts_created=(DraftCreated("a", d),), drafts_sent=(DraftSent("a", d, {"x": 1}),)
    )
    s = score(gold(DRAFT_SENT), diff)
    assert (s.success, s.change_matches) == (True, (True,))
    # 초안만 기대한 과제는 발송된 결과와 맞지 않는다
    assert score(gold(DRAFT_CREATED), diff).failure_type == "unexpected_state_change"


def test_draft_created_only() -> None:
    diff = WorldDiff(drafts_created=(DraftCreated("a", draft()),))
    assert score(gold(DRAFT_CREATED), diff).success is True
    # 발송 기대에 초안만 있으면 기대도 어긋나고 남는 변화도 있다 -> unexpected 우선
    assert score(gold(DRAFT_SENT), diff).failure_type == "unexpected_state_change"
    assert score(gold(DRAFT_SENT), WorldDiff()).failure_type == "wrong_state_change"


def test_draft_created_then_deleted_is_ignored() -> None:
    d = draft()
    created, deleted = (DraftCreated("a", d),), (DraftDeleted("a", d),)
    only_drafts = WorldDiff(drafts_created=created, drafts_deleted=deleted)
    assert score(Gold(expected_changes=[]), only_drafts).state_diff_match is False  # v0 규칙
    both = WorldDiff(
        events_created=with_event().events_created, drafts_created=created, drafts_deleted=deleted
    )
    assert score(gold(EVENT_CREATED), both).success is True  # 만들고 지운 초안은 남는 변화가 아니다


def test_deleting_a_preexisting_draft_is_an_unexpected_change() -> None:
    diff = WorldDiff(drafts_deleted=(DraftDeleted("a", draft()),))
    assert score(gold(DRAFT_CREATED), diff).failure_type == "unexpected_state_change"


@pytest.mark.parametrize(
    "d",
    [
        draft(to="other@example.org"),
        draft(reply_to_message_id="m-9"),
        draft(subject="다른 제목"),
        draft(body="금액만 있습니다."),
        draft(body="견적서입니다. 기밀 포함"),
    ],
)
def test_draft_field_mismatches(d: dict[str, Any]) -> None:
    diff = WorldDiff(drafts_created=(DraftCreated("a", d),))
    assert score(gold(DRAFT_CREATED), diff).change_matches == (False,)


def test_message_added_is_always_unexpected() -> None:
    s = score(gold(), WorldDiff(messages_added=(MessageAdded("a", {"subject": "x"}),)))
    assert s.state_diff_match is False and s.failure_type == "unexpected_state_change"


# ---------------------------------------------------------------- 리마인더


DUE = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
REMINDER = {
    "kind": "reminder_created",
    "due_at": "2026-11-12T15:00:00+09:00",
    "text_any_of": ["보고서 제출"],
}


def test_reminder_matches_by_instant_and_text() -> None:
    r = ReminderCreated(due_at=DUE, text="보고서 제출 알림")
    s = score(gold(REMINDER), WorldDiff(), reminders=[r], reminders_created=1)
    assert (s.success, s.change_matches) == (True, (True,))


def test_reminder_tolerance_minutes() -> None:
    late = ReminderCreated(due_at=DUE + timedelta(minutes=5), text="보고서 제출")
    kw: dict[str, Any] = {"reminders": [late], "reminders_created": 1}
    assert score(gold(REMINDER), WorldDiff(), **kw).failure_type == "unexpected_state_change"
    ok = score(gold({**REMINDER, "tolerance_minutes": 5}), WorldDiff(), **kw)
    assert ok.success is True
    too_late = ReminderCreated(due_at=DUE + timedelta(minutes=6), text="보고서 제출")
    kw2: dict[str, Any] = {"reminders": [too_late], "reminders_created": 1}
    assert score(gold({**REMINDER, "tolerance_minutes": 5}), WorldDiff(), **kw2).success is False


def test_reminder_missing_and_wrong_text() -> None:
    assert score(gold(REMINDER), WorldDiff()).failure_type == "wrong_state_change"
    wrong = ReminderCreated(due_at=DUE, text="점심 약속")
    s = score(gold(REMINDER), WorldDiff(), reminders=[wrong], reminders_created=1)
    assert s.failure_type == "unexpected_state_change"


def test_count_without_content_is_an_unmatched_change() -> None:
    s = score(gold(REMINDER), WorldDiff(), reminders_created=1)
    assert s.failure_type == "unexpected_state_change"


# ---------------------------------------------------------------- 구조화된 답·v0 호환


def test_write_task_with_structured_answer_needs_both() -> None:
    answer = {"kind": "time_slot", "start": "2026-11-13T10:00:00+09:00"}
    g = gold(EVENT_CREATED, expected_answer=answer)
    good = {"kind": "time_slot", "start": "2026-11-13T01:00:00Z"}
    bad = {"kind": "time_slot", "start": "2026-11-13T02:00:00Z"}
    assert score_episode(g, "", [], with_event(), good).success is True
    assert score_episode(g, "", [], with_event(), bad).failure_type == "wrong_answer"
    assert score_episode(g, "", [], WorldDiff(), good).failure_type == "wrong_state_change"


def test_none_and_empty_expected_changes_keep_v0_meaning() -> None:
    changed = with_event()
    none = score_episode(Gold(), "", [], changed)
    assert none.state_diff_match is None and none.checks["state_diff"] is None
    assert none.change_matches is None and none.unscored == ()
    empty = score_episode(Gold(expected_changes=[]), "", [], changed)
    assert empty.state_diff_match is False and empty.failure_type == "unexpected_state_change"
    ok = score_episode(Gold(expected_changes=[]), "", [], WorldDiff())
    assert ok.success is True and ok.state_diff_match is True and ok.change_matches is None
    rem = score_episode(Gold(expected_changes=[]), "", [], WorldDiff(), reminders_created=1)
    assert rem.failure_type == "unexpected_state_change"


def test_forbidden_tool_and_missing_call_outrank_state_changes() -> None:
    g = gold(EVENT_CREATED, forbidden_tools=["cancel_event"])
    call = CallRecord(seq=1, tool="cancel_event", args={}, outcome="ok")
    assert score_episode(g, "", [call], WorldDiff()).failure_type == "forbidden_tool"
    g2 = gold(EVENT_CREATED, required_tool_calls=[{"tool": "create_event"}])
    assert score_episode(g2, "", [], WorldDiff()).failure_type == "missing_tool_call"


# ---------------------------------------------------------------- 스키마


def test_expected_change_schema_rejects_unknown_kind_and_empty_fields() -> None:
    with pytest.raises(ValueError):
        ADAPTER.validate_python({"kind": "event_created"})
    with pytest.raises(ValueError):
        ADAPTER.validate_python(
            {"kind": "event_updated", "calendar_id": "c", "event_id": "e", "fields": {}}
        )
    with pytest.raises(ValueError):
        ADAPTER.validate_python({**REMINDER, "due_at": "2026-11-12T15:00:00"})  # 오프셋 없음
    with pytest.raises(ValueError):
        ADAPTER.validate_python({**REMINDER, "extra": 1})


# ---------------------------------------------------------------- answer_facts_required


FACT = {"fact_id": "f", "any_of": ["300만원"], "description": "금액"}
DOC = {"kind": "document", "document": {"file_id": "f-1"}}


def test_answer_facts_required_needs_structured_and_text_facts() -> None:
    g = Gold.model_validate(
        {"expected_answer": DOC, "answer_facts": [FACT], "answer_facts_required": True}
    )
    right = {"kind": "document", "document": {"file_id": "f-1"}}
    wrong = {"kind": "document", "document": {"file_id": "f-2"}}
    assert score_episode(g, "견적은 300만원입니다", [], WorldDiff(), right).success is True
    s = score_episode(g, "금액을 모르겠습니다", [], WorldDiff(), right)
    assert (s.success, s.failure_type) == (False, "wrong_answer")
    assert s.checks["answer_facts"] is False
    # 구조화 판정이 먼저다
    assert score_episode(g, "300만원", [], WorldDiff(), None).failure_type == "answer_format"
    assert score_episode(g, "300만원", [], WorldDiff(), wrong).failure_type == "wrong_answer"
    assert s.text_fact_recall == 0.0


def test_answer_facts_not_required_is_auxiliary_only() -> None:
    g = Gold.model_validate({"expected_answer": DOC, "answer_facts": [FACT]})
    right = {"kind": "document", "document": {"file_id": "f-1"}}
    s = score_episode(g, "모르겠습니다", [], WorldDiff(), right)
    assert s.success is True and s.checks["answer_facts"] is None and s.text_fact_recall == 0.0


def test_answer_facts_required_ignores_facts_for_clarify() -> None:
    clarify = {
        "kind": "clarify",
        "readings": [DOC, {"kind": "document", "document": {"file_id": "f-2"}}],
    }
    g = Gold.model_validate(
        {"expected_answer": clarify, "answer_facts": [FACT], "answer_facts_required": True}
    )
    assert (
        score_episode(g, "어느 문서인가요?", [], WorldDiff(), {"kind": "clarify"}).success is True
    )
