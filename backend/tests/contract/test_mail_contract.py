"""MailConnector 계약 테스트 (ADR-0008 4·6절).

구현체 무관: 계약 메서드만 쓰고 내부 상태는 보지 않는다. 6단계에서 실제 Gmail 테스트 계정에도
같은 묶음을 돌리므로 Sim 전용 가정을 두지 않는다.
- 메시지 ID 일치는 가정하지 않는다(SPIKE-005 D4). 스레드 ID 일치만 검사한다.
- 정확한 결과 목록 대신 "포함/미포함"으로 단언한다(실제 계정에는 다른 메일이 있을 수 있다).
- 검색어는 Gmail 단어 단위 검색에서도 성립하도록 공백으로 나뉜 완전한 토큰이나 제목 전체를 쓴다.
- 부분 문자열·형태소 일치, 정확한 결과 목록, 초안 제외 같은 Sim 동작은 단위 테스트에서 검사한다.
- 직접 만든 항목이 검색에 나타나기를 기다려야 하는 단언(긍정)은 `wait_for_search` 헬퍼(픽스처
  `wait_search`)를 거친다. 부정 단언은 긍정 대조군이 먼저 색인된 것을 확인한 뒤 한다.
  Sim에서는 첫 시도에 통과하고 실제로 기다리지 않는다(sleep 주입).
- 계약: Adapter는 지원 부분집합 밖의 검색어를 서버 호출 전에 검증해 `InvalidQuery`를 던져야 한다
  (ADR-0008 4절). 아래 `InvalidQuery` 파라미터 목록이 그 계약이다.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from orbit.connectors.contracts import (
    ORBIT_EXECUTION_HEADER,
    Address,
    DraftSpec,
    InvalidQuery,
    MailConnector,
    MessageSummary,
    NotFound,
)
from orbit.connectors.sim.world import SimWorld

from .conftest import MAIL_FACTORIES, MailFactory

ALICE = "acct-alice"
BOB = "acct-bob"

# ---- 시드 데이터 (6단계 Google 테스트 계정에도 같은 시드가 있어야 한다) -----------------------
# 앨리스 계정(ALICE) 메일함:
#   받은편지함, 보낸 사람 bob@example.org, 제목 SUBJ_INQUIRY, 2026-11-05
#   보낸편지함, 받는 사람 bob@example.org, 제목 SUBJ_REPLY, 같은 스레드, 2026-11-05
#   받은편지함, 보낸 사람 bob@example.org, 제목 SUBJ_REPORT, 첨부 report.pdf, 2026-11-06
# 밥 계정(BOB) 보낸편지함: 받는 사람 alice@example.com, 제목 SUBJ_INQUIRY, 2026-11-05
# (날짜는 UTC 기준 오전 9~10시 근처라 시간대 경계 문제가 없도록 잡았다)
BOB_ADDR = "bob@example.org"
ALICE_ADDR = "alice@example.com"
SUBJ_INQUIRY = "프로젝트 일정 문의"
SUBJ_REPLY = "Re: 프로젝트 일정 문의"
SUBJ_REPORT = "분기 보고서 첨부"
ATTACHMENT_NAME = "report.pdf"
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def mail(mail_factory: MailFactory, minimal_world: SimWorld) -> MailConnector:
    return mail_factory(minimal_world, ALICE)


Predicate = Callable[[list[MessageSummary]], bool]
SleepFn = Callable[[float], Awaitable[None]]
WaitSearch = Callable[[MailConnector, str, Predicate], Awaitable[list[MessageSummary]]]


async def _no_sleep(_seconds: float) -> None:
    return None


async def wait_for_search(
    mail: MailConnector,
    query: str,
    predicate: Predicate,
    *,
    timeout_s: float = 10.0,
    interval_s: float = 0.5,
    sleep: SleepFn = asyncio.sleep,
) -> list[MessageSummary]:
    """`predicate`가 참이 될 때까지 검색을 반복한다. 시간 초과면 AssertionError.

    대기는 주입한 `sleep`으로 한다. 시각을 읽지 않고 시도 횟수로 제한한다.
    """
    attempts = int(timeout_s / interval_s) + 1
    found: list[MessageSummary] = []
    for attempt in range(attempts):
        found = list((await mail.search_messages(query, max_results=50)).messages)
        if predicate(found):
            return found
        if attempt < attempts - 1:
            await sleep(interval_s)
    raise AssertionError(f"{timeout_s}초 안에 {query!r} 검색 결과가 조건을 만족하지 않았다")


def has_subject(subject: str) -> Predicate:
    return lambda found: any(m.subject == subject for m in found)


@pytest.fixture
def wait_search(mail_factory: MailFactory) -> WaitSearch:
    # Sim은 기다리지 않는다. 실제 어댑터는 실제로 대기한다.
    sleep: SleepFn = _no_sleep if mail_factory is MAIL_FACTORIES.get("sim") else asyncio.sleep

    async def wait(mail: MailConnector, query: str, predicate: Predicate) -> list[MessageSummary]:
        return await wait_for_search(mail, query, predicate, sleep=sleep)

    return wait


async def subjects_of(mail: MailConnector, query: str) -> list[str]:
    page = await mail.search_messages(query, max_results=50)
    return [m.subject for m in page.messages]


async def find_one(mail: MailConnector, query: str, subject: str) -> MessageSummary:
    page = await mail.search_messages(query, max_results=50)
    hits = [m for m in page.messages if m.subject == subject]
    assert hits, f"{query!r} 검색에 {subject!r}가 없다"
    return hits[0]


def spec(**kw: object) -> DraftSpec:
    base: dict[str, object] = {
        "to": (Address(email=BOB_ADDR, name="밥"),),
        "subject": "계약 테스트 메일",
        "body_text": "계약 테스트 합성 본문",
        "execution_id": "exec-0001",
    }
    base.update(kw)
    return DraftSpec.model_validate(base)


# ------------------------------------------------------------------ 검색


async def test_from_operator(mail: MailConnector) -> None:
    found = await subjects_of(mail, f"from:{BOB_ADDR}")
    assert SUBJ_INQUIRY in found and SUBJ_REPORT in found
    assert SUBJ_REPLY not in found
    assert SUBJ_REPLY in await subjects_of(mail, f"FROM:{ALICE_ADDR.upper()}")


async def test_to_operator(mail: MailConnector) -> None:
    to_bob = await subjects_of(mail, f"to:{BOB_ADDR}")
    assert SUBJ_REPLY in to_bob
    assert SUBJ_INQUIRY not in to_bob
    to_alice = await subjects_of(mail, f"to:{ALICE_ADDR}")
    assert SUBJ_INQUIRY in to_alice and SUBJ_REPORT in to_alice
    assert SUBJ_REPLY not in to_alice


async def test_subject_operator_and_quoted_value(mail: MailConnector) -> None:
    assert SUBJ_REPORT in await subjects_of(mail, "subject:보고서")
    assert SUBJ_INQUIRY not in await subjects_of(mail, "subject:보고서")
    phrase = await subjects_of(mail, 'subject:"일정 문의"')
    assert SUBJ_INQUIRY in phrase and SUBJ_REPLY in phrase
    assert SUBJ_REPORT not in phrase
    assert SUBJ_INQUIRY in await subjects_of(mail, f'subject:"{SUBJ_INQUIRY}"')


async def test_date_operators(mail: MailConnector) -> None:
    after = await subjects_of(mail, "after:2026/11/06")
    assert SUBJ_REPORT in after
    assert SUBJ_INQUIRY not in after and SUBJ_REPLY not in after
    before = await subjects_of(mail, "before:2026/11/06")
    assert SUBJ_INQUIRY in before and SUBJ_REPLY in before
    assert SUBJ_REPORT not in before
    window = await subjects_of(mail, "after:2026/11/05 before:2026/11/06")
    assert SUBJ_INQUIRY in window and SUBJ_REPLY in window
    assert SUBJ_REPORT not in window
    future = await subjects_of(mail, "after:2026/11/07")
    assert not {SUBJ_INQUIRY, SUBJ_REPLY, SUBJ_REPORT} & set(future)


async def test_has_attachment(mail: MailConnector) -> None:
    found = await subjects_of(mail, "has:attachment")
    assert SUBJ_REPORT in found
    assert SUBJ_INQUIRY not in found and SUBJ_REPLY not in found


async def test_in_operators(mail: MailConnector) -> None:
    inbox = await subjects_of(mail, "in:inbox")
    assert SUBJ_INQUIRY in inbox and SUBJ_REPORT in inbox
    assert SUBJ_REPLY not in inbox
    sent = await subjects_of(mail, "in:sent")
    assert SUBJ_REPLY in sent
    assert SUBJ_INQUIRY not in sent


async def test_results_are_newest_first(mail: MailConnector) -> None:
    page = await mail.search_messages("in:inbox", max_results=50)
    times = [m.sent_at for m in page.messages]
    assert len(times) >= 2
    assert times == sorted(times, reverse=True)


async def test_keywords_and_phrase_match_subject_or_body(
    mail: MailConnector, wait_search: WaitSearch
) -> None:
    token = "zebraquartz"
    subject = f"키워드 {token} 제목"
    draft = await mail.create_draft(spec(subject=subject, body_text="alpha beta gamma delta"))
    await mail.send_draft(draft.draft_id)
    await wait_search(mail, token, has_subject(subject))  # 제목 토큰
    await wait_search(mail, "gamma", has_subject(subject))  # 본문 토큰
    await wait_search(mail, f'"alpha beta" {token}', has_subject(subject))
    # 긍정 대조군이 색인된 뒤의 부정 단언
    assert subject not in await subjects_of(mail, f'"beta alpha" {token}')
    assert subject not in await subjects_of(mail, f"{token} missingtokenxyz")


async def test_conditions_are_anded_and_case_insensitive(mail: MailConnector) -> None:
    found = await subjects_of(mail, f"from:{BOB_ADDR.upper()} in:inbox has:attachment")
    assert SUBJ_REPORT in found
    assert SUBJ_INQUIRY not in found
    none = await subjects_of(mail, f"from:{BOB_ADDR} in:sent")
    assert not {SUBJ_INQUIRY, SUBJ_REPORT} & set(none)


async def test_in_drafts_finds_created_draft(mail: MailConnector, wait_search: WaitSearch) -> None:
    await mail.create_draft(spec(subject="유일한초안제목 zebradraft"))
    await wait_search(mail, "in:drafts zebradraft", has_subject("유일한초안제목 zebradraft"))


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


async def test_pagination_walks_results_without_overlap(mail: MailConnector) -> None:
    query = f"from:{BOB_ADDR}"  # 시드에 두 통 이상
    seen: list[str] = []
    token: str | None = None
    for _ in range(50):
        page = await mail.search_messages(query, max_results=1, page_token=token)
        assert len(page.messages) <= 1
        seen.extend(m.id for m in page.messages)
        token = page.next_page_token
        if token is None:
            break
    assert token is None
    assert len(seen) >= 2
    assert len(seen) == len(set(seen))


async def test_summary_fields(mail: MailConnector) -> None:
    summary = await find_one(mail, "has:attachment", SUBJ_REPORT)
    assert summary.from_.email == BOB_ADDR
    assert summary.has_attachment is True
    assert "INBOX" in summary.labels
    assert summary.thread_id


# ------------------------------------------------------------------ 조회


async def test_get_message(mail: MailConnector) -> None:
    summary = await find_one(mail, "has:attachment", SUBJ_REPORT)
    msg = await mail.get_message(summary.id)
    assert msg.subject == SUBJ_REPORT
    assert msg.attachments[0].filename == ATTACHMENT_NAME


async def test_get_thread_contains_both_messages_oldest_first(mail: MailConnector) -> None:
    received = await find_one(mail, f"from:{BOB_ADDR}", SUBJ_INQUIRY)
    replied = await find_one(mail, "in:sent", SUBJ_REPLY)
    assert received.thread_id == replied.thread_id
    thread = await mail.get_thread(received.thread_id)
    ids = [m.id for m in thread.messages]
    assert received.id in ids and replied.id in ids
    times = [m.sent_at for m in thread.messages]
    assert times == sorted(times)


async def test_get_missing_message_and_thread_raise_not_found(mail: MailConnector) -> None:
    with pytest.raises(NotFound):
        await mail.get_message("msg-does-not-exist")
    with pytest.raises(NotFound):
        await mail.get_thread("thr-does-not-exist")


async def test_other_accounts_messages_are_not_found(
    mail_factory: MailFactory, minimal_world: SimWorld
) -> None:
    alice = mail_factory(minimal_world, ALICE)
    bob = mail_factory(minimal_world, BOB)
    bobs = await find_one(bob, "in:sent", SUBJ_INQUIRY)
    with pytest.raises(NotFound):
        await alice.get_message(bobs.id)
    with pytest.raises(NotFound):
        await alice.get_thread(bobs.thread_id)


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
    origin = await find_one(mail, "has:attachment", SUBJ_REPORT)
    draft = await mail.create_draft(spec(reply_to_message_id=origin.id))
    assert draft.thread_id == origin.thread_id


async def test_get_missing_draft_raises_not_found(mail: MailConnector) -> None:
    with pytest.raises(NotFound):
        await mail.get_draft("drf-does-not-exist")


async def test_send_draft_full_flow(mail: MailConnector, wait_search: WaitSearch) -> None:
    draft = await mail.create_draft(spec())
    sent = await mail.send_draft(draft.draft_id)

    assert "SENT" in sent.labels
    assert sent.thread_id == draft.thread_id  # 스레드 ID는 유지된다 (SPIKE-005 D4)
    assert sent.headers[ORBIT_EXECUTION_HEADER] == "exec-0001"  # 헤더 보존 (D5)
    assert sent.subject == "계약 테스트 메일"

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

    # 보낸편지함 검색으로도 찾을 수 있다.
    hits = await wait_search(
        mail,
        "in:sent",
        lambda found: any(
            m.subject == "계약 테스트 메일" and m.thread_id == draft.thread_id for m in found
        ),
    )
    found = [m for m in hits if m.subject == "계약 테스트 메일" and m.thread_id == draft.thread_id]
    assert len(found) == 1
    assert await mail.get_message(found[0].id) is not None


async def test_send_reply_draft_joins_existing_thread(mail: MailConnector) -> None:
    origin = await find_one(mail, "has:attachment", SUBJ_REPORT)
    draft = await mail.create_draft(spec(reply_to_message_id=origin.id))
    sent = await mail.send_draft(draft.draft_id)
    assert sent.thread_id == origin.thread_id
    ids = {m.id for m in (await mail.get_thread(origin.thread_id)).messages}
    assert sent.id in ids and origin.id in ids


async def test_send_to_self_also_gets_inbox_label(mail: MailConnector) -> None:
    draft = await mail.create_draft(spec(to=(Address(email=ALICE_ADDR),)))
    sent = await mail.send_draft(draft.draft_id)
    assert "SENT" in sent.labels
    # SPIKE-005 실측: 본인에게 보낸 `drafts.send` 응답의 labelIds가 UNREAD, SENT, INBOX였다.
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


async def test_deleted_draft_leaves_no_sent_mail(
    mail: MailConnector, wait_search: WaitSearch
) -> None:
    deleted = await mail.create_draft(spec(subject="삭제될초안제목 zebradeleted"))
    control = await mail.create_draft(spec(subject="대조군메일제목 zebradeleted"))
    await mail.delete_draft(deleted.draft_id)
    await mail.send_draft(control.draft_id)
    # 대조군이 색인된 것을 확인한 뒤에 삭제된 초안의 부재를 단언한다.
    found = await wait_search(
        mail, "in:sent zebradeleted", has_subject("대조군메일제목 zebradeleted")
    )
    assert "삭제될초안제목 zebradeleted" not in [m.subject for m in found]


async def test_other_accounts_drafts_and_sent_messages_are_not_found(
    mail_factory: MailFactory, minimal_world: SimWorld
) -> None:
    alice = mail_factory(minimal_world, ALICE)
    bob = mail_factory(minimal_world, BOB)
    draft = await alice.create_draft(spec())
    for op in (bob.get_draft, bob.send_draft, bob.delete_draft):
        with pytest.raises(NotFound):
            await op(draft.draft_id)
    # 앨리스의 초안은 그대로 있고, 발송한 메시지·스레드를 밥은 조회할 수 없다.
    assert (await alice.get_draft(draft.draft_id)).draft_id == draft.draft_id
    sent = await alice.send_draft(draft.draft_id)
    with pytest.raises(NotFound):
        await bob.get_message(sent.id)
    with pytest.raises(NotFound):
        await bob.get_thread(sent.thread_id)
