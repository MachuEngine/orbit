"""MailConnector 계약 테스트 (ADR-0008 4·6절).

구현체 무관: 계약 메서드만 쓰고 내부 상태는 보지 않는다.

메시지 ID 일치는 가정하지 않는다(SPIKE-005 D4). 스레드 ID 일치만 검사한다.
"""

import pytest

from orbit.connectors.contracts import (
    ORBIT_EXECUTION_HEADER,
    Address,
    DraftSpec,
    InvalidQuery,
    MailConnector,
    NotFound,
)
from orbit.connectors.sim.world import SimWorld

from .conftest import MailFactory

ALICE = "acct-alice"
BOB = "acct-bob"


@pytest.fixture
def mail(mail_factory: MailFactory, minimal_world: SimWorld) -> MailConnector:
    return mail_factory(minimal_world, ALICE)


async def ids_of(mail: MailConnector, query: str) -> list[str]:
    page = await mail.search_messages(query, max_results=50)
    return [m.id for m in page.messages]


def spec(**kw: object) -> DraftSpec:
    base: dict[str, object] = {
        "to": (Address(email="bob@example.org", name="밥"),),
        "subject": "계약 테스트 메일",
        "body_text": "계약 테스트 합성 본문",
        "execution_id": "exec-0001",
    }
    base.update(kw)
    return DraftSpec.model_validate(base)


# ------------------------------------------------------------------ 검색


async def test_empty_query_returns_all_non_draft_messages_newest_first(
    mail: MailConnector,
) -> None:
    assert await ids_of(mail, "") == ["msg-a-003", "msg-a-002", "msg-a-001"]


async def test_from_operator(mail: MailConnector) -> None:
    assert await ids_of(mail, "from:bob@example.org") == ["msg-a-003", "msg-a-001"]
    assert await ids_of(mail, "FROM:ALICE") == ["msg-a-002"]


async def test_to_operator(mail: MailConnector) -> None:
    assert await ids_of(mail, "to:bob@example.org") == ["msg-a-002"]
    assert await ids_of(mail, "to:alice@example.com") == ["msg-a-003", "msg-a-001"]


async def test_subject_operator_and_quoted_value(mail: MailConnector) -> None:
    assert await ids_of(mail, "subject:보고서") == ["msg-a-003"]
    assert await ids_of(mail, 'subject:"일정 문의"') == ["msg-a-002", "msg-a-001"]
    assert await ids_of(mail, 'subject:"일정문의"') == []


async def test_date_operators(mail: MailConnector) -> None:
    assert await ids_of(mail, "after:2026/11/06") == ["msg-a-003"]
    assert await ids_of(mail, "before:2026/11/06") == ["msg-a-002", "msg-a-001"]
    assert await ids_of(mail, "after:2026/11/05 before:2026/11/06") == ["msg-a-002", "msg-a-001"]
    assert await ids_of(mail, "after:2026/11/07") == []


async def test_has_attachment(mail: MailConnector) -> None:
    assert await ids_of(mail, "has:attachment") == ["msg-a-003"]


async def test_in_operators(mail: MailConnector) -> None:
    assert await ids_of(mail, "in:inbox") == ["msg-a-003", "msg-a-001"]
    assert await ids_of(mail, "in:sent") == ["msg-a-002"]


async def test_in_drafts_returns_only_drafts(mail: MailConnector) -> None:
    drafts = await mail.search_messages("in:drafts")
    assert len(drafts.messages) == 1
    assert drafts.messages[0].subject == "Re: 프로젝트 일정 문의"
    # in:drafts가 아니면 초안은 검색에 나오지 않는다.
    assert drafts.messages[0].id not in await ids_of(mail, "")
    assert drafts.messages[0].id not in await ids_of(mail, "일정")


async def test_keywords_match_subject_body_or_sender(mail: MailConnector) -> None:
    assert await ids_of(mail, "첨부합니다") == ["msg-a-003"]  # 본문
    assert await ids_of(mail, "분기") == ["msg-a-003"]  # 제목
    assert await ids_of(mail, "alice@example.com") == ["msg-a-002"]  # 발신자
    assert await ids_of(mail, "화요일") == ["msg-a-002"]
    assert await ids_of(mail, "존재하지않는단어") == []


async def test_quoted_phrase(mail: MailConnector) -> None:
    assert await ids_of(mail, '"다음 주 일정"') == ["msg-a-001"]
    assert await ids_of(mail, '"주 다음 일정"') == []


async def test_conditions_are_anded_and_case_insensitive(mail: MailConnector) -> None:
    assert await ids_of(mail, "from:BOB in:inbox has:attachment") == ["msg-a-003"]
    assert await ids_of(mail, "from:bob in:sent") == []
    assert await ids_of(mail, "일정 FROM:Bob after:2026/11/01") == ["msg-a-001"]


@pytest.mark.parametrize(
    "query",
    [
        "a OR b",
        "-from:bob",
        "-스팸",
        "label:important",
        "rfc822msgid:abc",
        "is:unread",
        "(a b)",
        "{a b}",
        "unknownop:value",
        "has:drive",
        "in:trash",
        "after:2026-11-05",
        "after:2026/13/01",
        "before:abc",
        "after:",
        "from:",
        '"닫히지 않은',
    ],
)
async def test_unsupported_or_malformed_query_raises_invalid_query(
    mail: MailConnector, query: str
) -> None:
    with pytest.raises(InvalidQuery):
        await mail.search_messages(query)


async def test_pagination_walks_all_results_without_overlap(mail: MailConnector) -> None:
    first = await mail.search_messages("", max_results=2)
    assert [m.id for m in first.messages] == ["msg-a-003", "msg-a-002"]
    assert first.next_page_token is not None
    second = await mail.search_messages("", max_results=2, page_token=first.next_page_token)
    assert [m.id for m in second.messages] == ["msg-a-001"]
    assert second.next_page_token is None


async def test_pagination_exact_fit_has_no_next_page(mail: MailConnector) -> None:
    page = await mail.search_messages("", max_results=3)
    assert len(page.messages) == 3
    assert page.next_page_token is None


async def test_summary_fields(mail: MailConnector) -> None:
    page = await mail.search_messages("subject:보고서")
    (summary,) = page.messages
    assert summary.thread_id == "thr-a-002"
    assert summary.from_.email == "bob@example.org"
    assert summary.has_attachment is True
    assert "INBOX" in summary.labels


# ------------------------------------------------------------------ 조회


async def test_get_message_and_thread(mail: MailConnector) -> None:
    msg = await mail.get_message("msg-a-003")
    assert msg.subject == "분기 보고서 첨부"
    assert msg.attachments[0].filename == "report.pdf"
    thread = await mail.get_thread("thr-a-001")
    assert {m.id for m in thread.messages} == {"msg-a-001", "msg-a-002"}
    assert [m.id for m in thread.messages] == ["msg-a-001", "msg-a-002"]  # 오래된 순


async def test_get_missing_message_and_thread_raise_not_found(mail: MailConnector) -> None:
    with pytest.raises(NotFound):
        await mail.get_message("msg-does-not-exist")
    with pytest.raises(NotFound):
        await mail.get_thread("thr-does-not-exist")


async def test_other_accounts_ids_are_not_found(mail: MailConnector) -> None:
    with pytest.raises(NotFound):
        await mail.get_message("msg-b-001")
    with pytest.raises(NotFound):
        await mail.get_thread("thr-b-001")
    assert "msg-b-001" not in await ids_of(mail, "")


# ------------------------------------------------------------------ 초안과 발송


async def test_create_and_get_draft(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec())
    assert draft.draft_id and draft.message_id and draft.thread_id
    got = await mail.get_draft(draft.draft_id)
    assert got.draft_id == draft.draft_id
    assert got.thread_id == draft.thread_id
    assert got.spec is not None
    assert got.spec.subject == "계약 테스트 메일"
    assert got.spec.headers[ORBIT_EXECUTION_HEADER] == "exec-0001"


async def test_new_draft_ids_are_distinct(mail: MailConnector) -> None:
    a = await mail.create_draft(spec())
    b = await mail.create_draft(spec())
    assert len({a.draft_id, b.draft_id}) == 2
    assert len({a.thread_id, b.thread_id}) == 2  # 답장이 아니면 각자 새 스레드


async def test_reply_draft_uses_origin_thread(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec(reply_to_message_id="msg-a-003"))
    assert draft.thread_id == "thr-a-002"


async def test_reply_to_missing_message_raises_not_found(mail: MailConnector) -> None:
    with pytest.raises(NotFound):
        await mail.create_draft(spec(reply_to_message_id="msg-does-not-exist"))


async def test_get_missing_draft_raises_not_found(mail: MailConnector) -> None:
    with pytest.raises(NotFound):
        await mail.get_draft("drf-does-not-exist")


async def test_created_draft_is_searchable_only_with_in_drafts(mail: MailConnector) -> None:
    await mail.create_draft(spec(subject="유일한초안제목"))
    assert await ids_of(mail, "유일한초안제목") == []
    page = await mail.search_messages("in:drafts 유일한초안제목")
    assert len(page.messages) == 1


async def test_send_draft_full_flow(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec())
    sent = await mail.send_draft(draft.draft_id)

    assert "SENT" in sent.labels
    assert sent.thread_id == draft.thread_id  # 스레드 ID는 유지된다 (SPIKE-005 D4)
    assert sent.headers[ORBIT_EXECUTION_HEADER] == "exec-0001"  # 헤더 보존 (D5)
    assert sent.subject == "계약 테스트 메일"
    assert sent.from_.email == "alice@example.com"

    # 초안은 사라진다 (D1). 발송 후 다시 발송해도 거절된다 (D3).
    with pytest.raises(NotFound):
        await mail.get_draft(draft.draft_id)
    with pytest.raises(NotFound):
        await mail.send_draft(draft.draft_id)

    # 대조 절차: 스레드 조회로 SENT 메일을 찾는다 (D6). 메시지 ID 일치는 가정하지 않는다.
    thread = await mail.get_thread(draft.thread_id)
    matched = [
        m
        for m in thread.messages
        if "SENT" in m.labels and m.headers.get(ORBIT_EXECUTION_HEADER) == "exec-0001"
    ]
    assert len(matched) == 1

    # 보낸편지함 검색으로도 찾을 수 있고, 두 번 발송되지 않았다.
    found = await mail.search_messages("in:sent 계약 테스트 메일")
    assert len(found.messages) == 1
    assert found.messages[0].thread_id == draft.thread_id
    assert await mail.get_message(found.messages[0].id) is not None


async def test_send_reply_draft_joins_existing_thread(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec(reply_to_message_id="msg-a-003"))
    sent = await mail.send_draft(draft.draft_id)
    assert sent.thread_id == "thr-a-002"
    thread = await mail.get_thread("thr-a-002")
    assert sent.id in {m.id for m in thread.messages}
    assert "msg-a-003" in {m.id for m in thread.messages}


async def test_send_to_self_also_gets_inbox_label(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec(to=(Address(email="alice@example.com"),)))
    sent = await mail.send_draft(draft.draft_id)
    assert "SENT" in sent.labels
    assert "INBOX" in sent.labels


async def test_send_to_other_has_no_inbox_label(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec())
    sent = await mail.send_draft(draft.draft_id)
    assert "INBOX" not in sent.labels


async def test_delete_draft_then_get_and_send_raise_not_found(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec())
    await mail.delete_draft(draft.draft_id)
    with pytest.raises(NotFound):
        await mail.get_draft(draft.draft_id)
    with pytest.raises(NotFound):
        await mail.send_draft(draft.draft_id)
    with pytest.raises(NotFound):
        await mail.delete_draft(draft.draft_id)


async def test_delete_missing_draft_raises_not_found(mail: MailConnector) -> None:
    with pytest.raises(NotFound):
        await mail.delete_draft("drf-does-not-exist")


async def test_deleted_draft_leaves_no_sent_mail(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec(subject="삭제될초안제목"))
    await mail.delete_draft(draft.draft_id)
    assert await ids_of(mail, "삭제될초안제목") == []
    assert (await mail.search_messages("in:drafts 삭제될초안제목")).messages == ()


async def test_other_accounts_drafts_are_not_found(
    mail_factory: MailFactory, minimal_world: SimWorld
) -> None:
    alice = mail_factory(minimal_world, ALICE)
    bob = mail_factory(minimal_world, BOB)
    draft = await alice.create_draft(spec())
    for op in (bob.get_draft, bob.send_draft, bob.delete_draft):
        with pytest.raises(NotFound):
            await op(draft.draft_id)
    with pytest.raises(NotFound):
        await bob.get_draft("drf-a-001")
    # 앨리스의 초안은 그대로 있다.
    assert (await alice.get_draft(draft.draft_id)).draft_id == draft.draft_id


async def test_send_does_not_leak_into_other_account(
    mail_factory: MailFactory, minimal_world: SimWorld
) -> None:
    alice = mail_factory(minimal_world, ALICE)
    bob = mail_factory(minimal_world, BOB)
    before = await ids_of(bob, "")
    draft = await alice.create_draft(spec(subject="앨리스만보는제목"))
    sent = await alice.send_draft(draft.draft_id)
    assert await ids_of(bob, "") == before
    with pytest.raises(NotFound):
        await bob.get_message(sent.id)
