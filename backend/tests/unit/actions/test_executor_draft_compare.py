"""`_same_draft`의 본문·제목 정규화 (Gmail 어댑터의 본문 규칙과 정합)."""

import unicodedata

from orbit.actions.executor import _same_draft
from orbit.actions.models import SendMail
from orbit.connectors.contracts import Address, DraftSpec


def action(**kw: object) -> SendMail:
    base: dict[str, object] = {
        "to": ("bob@example.org",),
        "subject": "제목",
        "body": "첫 줄\n둘째 줄",
    }
    base.update(kw)
    return SendMail.model_validate(base)


def spec(**kw: object) -> DraftSpec:
    base: dict[str, object] = {
        "to": (Address(email="bob@example.org"),),
        "subject": "제목",
        "body_text": "첫 줄\n둘째 줄",
    }
    base.update(kw)
    return DraftSpec.model_validate(base)


def test_trailing_newline_and_crlf_differences_are_the_same_draft() -> None:
    assert _same_draft(spec(body_text="첫 줄\r\n둘째 줄\r\n"), action())
    assert _same_draft(spec(), action(body="첫 줄\n둘째 줄\n\n"))
    assert _same_draft(spec(body_text="첫 줄\r둘째 줄"), action())


def test_real_body_change_is_a_different_draft() -> None:
    assert not _same_draft(spec(body_text="첫 줄\n둘째 줄 수정"), action())
    assert not _same_draft(spec(body_text="첫 줄\n\n둘째 줄"), action())
    assert not _same_draft(spec(body_text=" 첫 줄\n둘째 줄"), action())


def test_subject_is_stripped_and_nfc_normalized() -> None:
    nfd = unicodedata.normalize("NFD", "한글 제목")
    assert _same_draft(spec(subject=f"  {nfd} "), action(subject="한글 제목"))
    assert not _same_draft(spec(subject="다른 제목"), action(subject="제목"))


def test_unknown_spec_is_not_the_same() -> None:
    assert not _same_draft(None, action())
