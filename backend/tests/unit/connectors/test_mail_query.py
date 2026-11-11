from datetime import UTC, datetime

import pytest

from orbit.connectors.contracts import Address, AttachmentMeta, InvalidQuery, MailMessage
from orbit.connectors.sim.mail_query import Query, matches, parse_query

ME = "alice@example.com"


def msg(**kw: object) -> MailMessage:
    base: dict[str, object] = {
        "id": "m1",
        "thread_id": "t1",
        "from": Address(email="bob@example.org", name="Bob Kim"),
        "to": (Address(email="alice@example.com", name="Alice"),),
        "cc": (Address(email="carol@example.net", name="Carol"),),
        "subject": "Quarterly Report",
        "body_text": "Please see the attached numbers.",
        "sent_at": datetime(2026, 11, 5, 23, 59, tzinfo=UTC),
        "labels": ("INBOX",),
    }
    base.update(kw)
    return MailMessage.model_validate(base)


def ok(query: str, m: MailMessage | None = None) -> bool:
    return matches(parse_query(query), m or msg(), ME)


def test_empty_query_matches_everything() -> None:
    assert parse_query("") == Query()
    assert parse_query("   ") == Query()
    assert ok("")


def test_from_matches_email_or_name_case_insensitive() -> None:
    assert ok("from:bob@example.org")
    assert ok("from:BOB")
    assert ok("from:kim")
    assert not ok("from:carol")


def test_to_includes_cc() -> None:
    assert ok("to:alice")
    assert ok("to:carol@example.net")  # cc 포함
    assert not ok("to:bob")


def test_me_means_account_address() -> None:
    assert ok("to:me")
    assert not ok("from:me")
    assert ok("from:me", msg(**{"from": Address(email=ME)}))


def test_subject_and_quoted_value() -> None:
    assert ok("subject:quarterly")
    assert ok('subject:"quarterly report"')
    assert not ok('subject:"report quarterly"')
    assert not ok("subject:numbers")  # 본문에만 있다


def test_keywords_search_subject_body_sender() -> None:
    assert ok("quarterly")
    assert ok("numbers")
    assert ok("bob@example.org")
    assert ok("Kim")
    assert not ok("carol")  # 수신자는 일반 키워드 대상이 아니다
    assert ok('"attached numbers"')
    assert not ok('"numbers attached"')
    assert ok("quarterly numbers")
    assert not ok("quarterly missing")


def test_quoted_phrase_may_contain_operator_chars() -> None:
    assert ok('"a-b"', msg(body_text="x a-b y"))
    assert ok('"(x) OR y"', msg(body_text="see (x) OR y here"))


def test_date_boundaries_are_utc_midnight() -> None:
    m = msg()  # 2026-11-05 23:59 UTC
    assert ok("after:2026/11/05", m)  # 포함
    assert not ok("after:2026/11/06", m)
    assert ok("before:2026/11/06", m)
    assert not ok("before:2026/11/05", m)  # 미포함
    midnight = msg(sent_at=datetime(2026, 11, 6, tzinfo=UTC))
    assert ok("after:2026/11/06", midnight)
    assert not ok("before:2026/11/06", midnight)


def test_has_attachment() -> None:
    att = AttachmentMeta(
        attachment_id="a", filename="f.pdf", mime_type="application/pdf", size_bytes=1
    )
    assert not ok("has:attachment")
    assert ok("has:attachment", msg(attachments=(att,)))
    assert ok("HAS:Attachment", msg(attachments=(att,)))


def test_in_labels() -> None:
    assert ok("in:inbox")
    assert not ok("in:sent")
    assert ok("in:sent", msg(labels=("SENT",)))
    assert ok("in:drafts", msg(labels=("DRAFT",)))
    assert parse_query("in:drafts").wants_drafts
    assert not parse_query("in:inbox").wants_drafts


def test_all_conditions_are_anded() -> None:
    assert ok("from:bob in:inbox after:2026/11/01 before:2026/11/10 quarterly")
    assert not ok("from:bob in:inbox quarterly missing")


def test_operator_names_are_case_insensitive() -> None:
    assert parse_query("FROM:Bob") == parse_query("from:bob")


@pytest.mark.parametrize(
    "query",
    [
        "a OR b",
        "OR",
        "-x",
        "-from:bob",
        "from:-bob",
        "label:x",
        "rfc822msgid:x",
        "is:unread",
        "xxx:y",
        "10:30",
        "(a)",
        "a)",
        "{a b}",
        "from:(a b)",
        "has:drive",
        "has:",
        "in:trash",
        "in:",
        "after:2026-11-05",
        "after:2026/1/5",
        "after:2026/02/30",
        "after:20261105",
        "after:2026/11/05/01",
        "before:",
        "after:abc",
        "from:",
        'from:""',
        '""',
        '"open',
        'subject:"open',
        'a"b',
        '"a"b',
        ":x",
    ],
)
def test_invalid_queries(query: str) -> None:
    with pytest.raises(InvalidQuery):
        parse_query(query)


def test_lowercase_or_is_just_a_keyword() -> None:
    assert ok("or", msg(body_text="this or that"))
