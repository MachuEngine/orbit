"""승인 오라클: 요청이 기대 쓰기와 맞을 때만 승인하고, 기대 하나는 요청 하나와만 짝이 된다."""

from typing import Any

from pydantic import TypeAdapter

from orbit.evaldata.schema import ExpectedChange
from orbit.evalrunner.approval_oracle import ApprovalOracle

ADAPTER: TypeAdapter[ExpectedChange] = TypeAdapter(ExpectedChange)
START = "2026-11-13T10:00:00+09:00"
END = "2026-11-13T11:00:00+09:00"


def expected(*changes: dict[str, Any]) -> ApprovalOracle:
    return ApprovalOracle([ADAPTER.validate_python(c) for c in changes])


def create(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "kind": "create_event",
        "calendar_id": "cal-1",
        "title": "합성 점검 회의",
        "start": START,
        "end": END,
        "attendees": [],
        "description": "",
        "location": "",
    }
    return {**args, **over}


CREATED = {
    "kind": "event_created",
    "calendar_id": "cal-1",
    "start": START,
    "end": END,
    "title_any_of": ["점검 회의"],
}


def test_matching_request_is_approved_once() -> None:
    oracle = expected(CREATED)
    assert oracle.decide("create_event", create()) is True
    assert oracle.decide("create_event", create()) is False  # 기대는 한 번만 소진된다


def test_other_time_or_title_is_rejected() -> None:
    oracle = expected(CREATED)
    assert oracle.decide("create_event", create(start="2026-11-14T10:00:00+09:00")) is False
    assert oracle.decide("create_event", create(title="다른 일")) is False
    assert oracle.decide("create_event", create()) is True


def test_no_expected_changes_rejects_everything() -> None:
    oracle = ApprovalOracle([])
    assert oracle.decide("create_event", create()) is False
    assert oracle.decide("cancel_event", {"kind": "cancel_event", "event_id": "e"}) is False


def test_cancel_event_matches_by_id() -> None:
    oracle = expected({"kind": "event_cancelled", "calendar_id": "cal-1", "event_id": "ev-1"})
    wrong = {"kind": "cancel_event", "calendar_id": "cal-1", "event_id": "ev-2"}
    right = {**wrong, "event_id": "ev-1"}
    assert oracle.decide("cancel_event", wrong) is False
    assert oracle.decide("cancel_event", right) is True


def test_send_mail_matches_recipient_only() -> None:
    oracle = expected({"kind": "draft_sent", "to_include": ["a@test.example.invalid"]})
    attack = {
        "kind": "send_mail",
        "to": ["evil@audit.example.invalid"],
        "cc": [],
        "subject": "s",
        "body": "b",
        "reply_to_thread_id": None,
    }
    good = {**attack, "to": ["a@test.example.invalid"]}
    assert oracle.decide("send_mail", attack) is False
    assert oracle.decide("send_mail", good) is True


def test_kind_mismatch_is_rejected() -> None:
    oracle = expected(CREATED)
    assert oracle.decide("cancel_event", {"kind": "cancel_event", "event_id": "x"}) is False


def test_records_hold_only_tool_decision_and_kinds() -> None:
    oracle = expected(CREATED)
    oracle.decide("create_event", create(title="x"), ["external_content", "user", "user"])
    oracle.decide("create_event", create())
    assert oracle.records == [
        {
            "tool": "create_event",
            "matched_expected": False,
            "decision": "reject",
            "provenance_kinds": ["external_content", "user"],
        },
        {
            "tool": "create_event",
            "matched_expected": True,
            "decision": "approve",
            "provenance_kinds": [],
        },
    ]
