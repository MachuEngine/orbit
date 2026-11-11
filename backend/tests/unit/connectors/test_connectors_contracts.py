import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from orbit.connectors.contracts import (
    ORBIT_EXECUTION_HEADER,
    Attendee,
    CalendarEvent,
    ConnectorError,
    DraftSpec,
    EventDraft,
    EventPatch,
    MailMessage,
    NotFound,
    RateLimited,
    ResultUnknown,
    event_id_from_uuid,
    is_valid_client_event_id,
    new_event_id,
    validate_client_event_id,
)
from orbit.core.ids import uuid7

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def test_errors_share_base() -> None:
    assert issubclass(NotFound, ConnectorError)
    assert issubclass(ResultUnknown, ConnectorError)
    assert RateLimited(2.5).retry_after_s == 2.5


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("abcde", True),
        ("a" * 1024, True),
        ("a" * 1025, False),
        ("abcd", False),
        ("abcdw", False),  # w는 base32hex 범위 밖
        ("ABCDE", False),
        ("abc-de", False),
        ("", False),
    ],
)
def test_client_event_id_validation(value: str, ok: bool) -> None:
    assert is_valid_client_event_id(value) is ok
    if not ok:
        with pytest.raises(ValueError, match="일정 ID"):
            validate_client_event_id(value)


def test_uuid_to_event_id_is_valid_hex_without_hyphen() -> None:
    u = uuid.UUID("0190a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b")
    assert event_id_from_uuid(u) == "0190a1b2c3d47e5f8a6b7c8d9e0f1a2b"
    assert is_valid_client_event_id(event_id_from_uuid(uuid7()))
    assert is_valid_client_event_id(new_event_id())


def test_naive_datetime_rejected() -> None:
    naive = datetime(2026, 11, 11, 9, 0)  # noqa: DTZ001  거부 검증용
    with pytest.raises(ValidationError):
        EventDraft(title="x", start=naive, end=NOW)
    with pytest.raises(ValidationError):
        EventPatch(start=naive)
    with pytest.raises(ValidationError):
        CalendarEvent(
            id="abcde",
            calendar_id="c",
            title="t",
            start=NOW,
            end=naive,
            version="v",
            updated_at=NOW,
        )
    with pytest.raises(ValidationError):
        MailMessage.model_validate(
            {
                "id": "m",
                "thread_id": "t",
                "from": {"email": "a@example.com"},
                "subject": "s",
                "body_text": "b",
                "sent_at": "2026-11-11T09:00:00",
            }
        )


def test_aware_non_utc_accepted_and_end_before_start_rejected() -> None:
    kst = timezone(timedelta(hours=9))
    draft = EventDraft(title="x", start=NOW.astimezone(kst), end=NOW + timedelta(hours=1))
    assert draft.start == NOW
    with pytest.raises(ValidationError):
        EventDraft(title="x", start=NOW, end=NOW - timedelta(hours=1))


def test_models_are_frozen() -> None:
    attendee = Attendee(email="a@example.com")
    with pytest.raises(ValidationError):
        attendee.email = "b@example.com"  # type: ignore[misc]


def test_event_patch_changes_only_specified_fields() -> None:
    patch = EventPatch(title="새 제목", attendees=())
    assert patch.changes() == {"title": "새 제목", "attendees": ()}
    assert EventPatch().changes() == {}


def test_draft_spec_execution_header() -> None:
    spec = DraftSpec.model_validate(
        {
            "to": [{"email": "a@example.com"}],
            "subject": "s",
            "body_text": "b",
            "execution_id": "ex1",
        }
    )
    assert spec.headers == {ORBIT_EXECUTION_HEADER: "ex1"}
    assert DraftSpec(to=(), subject="s", body_text="b").headers == {}
