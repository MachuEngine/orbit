"""쓰기 인자 필드별 일치(ADR-0029 §2, 보조 지표). 성공 판정은 바뀌지 않는다. 합성 데이터만 쓴다."""

from typing import Any

from orbit.connectors.sim.world import DraftCreated, EventCancelled, EventCreated, WorldDiff
from orbit.evalrunner.runflow_env import ReminderCreated

from .test_state_changes import (
    DRAFT_SENT,
    DUE,
    EVENT_CREATED,
    MOVE,
    REMINDER,
    UPDATED,
    created_event,
    draft,
    gold,
    score,
    updated,
    with_event,
)


def fields(s: Any, index: int = 0) -> dict[str, bool]:
    return dict(s.change_fields[index]["fields"])


def test_no_expected_changes_means_no_field_results() -> None:
    assert score(gold(), WorldDiff()).change_fields is None
    assert score(gold(EVENT_CREATED), with_event()).change_fields is not None


def test_event_created_all_fields_match() -> None:
    s = score(gold({**EVENT_CREATED, "location_any_of": ["본관"]}), with_event(location="본관 2층"))
    assert s.change_fields == (
        {
            "kind": "event_created",
            "fields": {"title": True, "time": True, "attendees": True, "location": True},
        },
    )


def test_event_created_reports_which_field_is_wrong_without_changing_success() -> None:
    wrong_time = score(gold(EVENT_CREATED), with_event(start="2026-11-13T02:00:00Z"))
    assert fields(wrong_time) == {"title": True, "time": False, "attendees": True}
    assert wrong_time.success is False  # 판정은 기존 규칙 그대로다
    wrong_title = score(gold(EVENT_CREATED), with_event(title="다른 일정", attendees=[]))
    assert fields(wrong_title) == {"title": False, "time": True, "attendees": False}


def test_event_created_without_actual_change_marks_every_expected_field_false() -> None:
    s = score(gold({**EVENT_CREATED, "location_any_of": ["본관"]}), WorldDiff())
    assert fields(s) == {"title": False, "time": False, "attendees": False, "location": False}


def test_best_candidate_is_the_actual_change_with_most_matching_fields() -> None:
    diff = WorldDiff(
        events_created=(
            EventCreated("a", created_event(title="무관", start="2026-11-13T05:00:00Z")),
            EventCreated("a", created_event(attendees=[])),
        )
    )
    s = score(gold(EVENT_CREATED), diff)
    assert fields(s) == {"title": True, "time": True, "attendees": False}


def test_event_updated_fields_and_same_event_preference() -> None:
    s = score(gold(UPDATED), updated(**MOVE))
    assert s.change_fields == ({"kind": "event_updated", "fields": {"time": True}},)
    only_start = score(gold(UPDATED), updated(start=MOVE["start"]))
    assert fields(only_start) == {"time": False}
    other_event = WorldDiff(events_updated=updated(**MOVE).events_updated)
    missing = score(gold({**UPDATED, "event_id": "ev-other"}), other_event)
    assert fields(missing) == {"time": True}  # 같은 일정이 없으면 같은 종류의 변화와 비교한다


def test_event_cancelled_has_no_fields() -> None:
    exp = {"kind": "event_cancelled", "calendar_id": "cal-1", "event_id": "ev-1"}
    s = score(gold(exp), WorldDiff(events_cancelled=(EventCancelled("a", "cal-1", "ev-1"),)))
    assert s.change_fields == ({"kind": "event_cancelled", "fields": {}},)


def test_draft_recipient_and_subject() -> None:
    diff = WorldDiff(drafts_created=(DraftCreated("a", draft()),))
    created = {**DRAFT_SENT, "kind": "draft_created"}
    assert fields(score(gold(created), diff)) == {"recipient": True, "title": True}
    wrong = WorldDiff(drafts_created=(DraftCreated("a", draft(to="other@example.org")),))
    assert fields(score(gold(created), wrong)) == {"recipient": False, "title": True}
    assert fields(score(gold(created), WorldDiff())) == {"recipient": False, "title": False}


def test_reminder_time_field() -> None:
    r = ReminderCreated(due_at=DUE, text="보고서 제출 알림")
    ok = score(gold(REMINDER), WorldDiff(), reminders=[r], reminders_created=1)
    assert fields(ok) == {"time": True}
    late = ReminderCreated(due_at=DUE.replace(hour=9), text="보고서 제출 알림")
    bad = score(gold(REMINDER), WorldDiff(), reminders=[late], reminders_created=1)
    assert fields(bad) == {"time": False}
