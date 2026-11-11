import unicodedata
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from orbit.actions.hashing import args_hash
from orbit.actions.models import CancelEvent, CreateEvent, SendMail, UpdateEventTime

T0 = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 12, 2, 0, tzinfo=UTC)
KST = timezone(timedelta(hours=9))


def create(**kw: object) -> CreateEvent:
    base: dict[str, object] = {
        "calendar_id": "cal",
        "title": "회의",
        "start": T0,
        "end": T1,
        "attendees": ("a@example.com", "b@example.com"),
    }
    base.update(kw)
    return CreateEvent(**base)


def mail(**kw: object) -> SendMail:
    base: dict[str, object] = {
        "to": ("a@example.com", "b@example.com"),
        "cc": (),
        "subject": "제목",
        "body": "본문",
    }
    base.update(kw)
    return SendMail(**base)


def test_hash_is_hex_sha256_and_stable() -> None:
    h = args_hash(create())
    assert len(h) == 64 and int(h, 16) >= 0
    assert h == args_hash(create())


def test_same_meaning_same_hash() -> None:
    base = args_hash(create())
    assert args_hash(create(start=T0.astimezone(KST), end=T1.astimezone(KST))) == base
    assert args_hash(create(attendees=("B@Example.com", "a@example.com"))) == base
    assert args_hash(create(attendees=("a@example.com", "b@example.com", "A@example.com"))) == base
    assert args_hash(create(description="", location=None)) == base
    nfd = unicodedata.normalize("NFD", "회의")
    assert nfd != "회의"
    assert args_hash(create(title=nfd)) == base


def test_different_meaning_different_hash() -> None:
    base = args_hash(create())
    variants = [
        create(title="회의2"),
        create(start=T0 + timedelta(minutes=1)),
        create(end=T1 + timedelta(minutes=1)),
        create(attendees=("a@example.com",)),
        create(calendar_id="cal2"),
        create(description="설명"),
        create(location="A"),
    ]
    hashes = {args_hash(v) for v in variants}
    assert base not in hashes and len(hashes) == len(variants)


def test_kind_is_part_of_hash() -> None:
    a = args_hash(UpdateEventTime(calendar_id="c", event_id="e1", start=T0, end=T1))
    b = args_hash(CancelEvent(calendar_id="c", event_id="e1"))
    assert a != b


def test_update_time_is_utc_normalized() -> None:
    a = UpdateEventTime(calendar_id="c", event_id="e1", start=T0, end=T1)
    b = UpdateEventTime(
        calendar_id="c", event_id="e1", start=T0.astimezone(KST), end=T1.astimezone(KST)
    )
    assert args_hash(a) == args_hash(b)


def test_mail_recipients_sorted_and_to_cc_distinct() -> None:
    base = args_hash(mail())
    assert args_hash(mail(to=("B@example.com", "a@example.com"))) == base
    assert args_hash(mail(to=("a@example.com",), cc=("b@example.com",))) != base
    assert args_hash(mail(subject="제목 ")) != base
    assert args_hash(mail(body="본문2")) != base
    assert args_hash(mail(reply_to_thread_id="thr-1")) != base


def test_models_are_frozen_and_require_aware_times() -> None:
    event = create()
    with pytest.raises(ValidationError):
        event.title = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        create(start=datetime(2026, 11, 12, 1, 0))  # noqa: DTZ001
    with pytest.raises(ValidationError):
        UpdateEventTime(calendar_id="c", event_id="e", start=datetime(2026, 1, 1), end=T1)  # noqa: DTZ001
    with pytest.raises(ValidationError):
        create(start=T1, end=T0)
    with pytest.raises(ValidationError):
        mail(to=())
    with pytest.raises(ValidationError):
        mail(to=("not-an-email",))
