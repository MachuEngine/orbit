from typing import Any

import pytest

from orbit.connectors.contracts import (
    ORBIT_EXECUTION_HEADER,
    DraftSpec,
    InvalidQuery,
    NotFound,
    ResultUnknown,
)
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.mail import SimMailConnector
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import SimWorld, SnapshotError, diff


def spec(**kw: Any) -> DraftSpec:
    base: dict[str, Any] = {
        "to": [{"email": "bob@example.org"}],
        "subject": "제목",
        "body_text": "본문",
        "execution_id": "exec-1",
    }
    base.update(kw)
    return DraftSpec.model_validate(base)


def connector(world: SimWorld, plan: dict[str, Any] | None = None) -> SimMailConnector:
    rt = SimRuntime(FaultPlan.from_dict(plan) if plan else None)
    return SimMailConnector(world, "acct-alice", rt)


# ---------------------------------------------------------------- 기본 동작


async def test_send_creates_new_message_id_same_thread(world: SimWorld) -> None:
    mail = connector(world)
    draft = await mail.create_draft(spec())
    sent = await mail.send_draft(draft.draft_id)
    assert sent.id != draft.message_id
    assert sent.thread_id == draft.thread_id
    assert sent.sent_at == world.clock.now()
    assert sent.headers == {ORBIT_EXECUTION_HEADER: "exec-1"}
    alice = world.account("acct-alice")
    assert sent.id in alice.threads[draft.thread_id]
    assert draft.draft_id not in alice.drafts


async def test_draft_message_id_is_not_found_after_send(world: SimWorld) -> None:
    mail = connector(world)
    draft = await mail.create_draft(spec())
    assert (await mail.get_message(draft.message_id)).labels == ("DRAFT",)
    await mail.send_draft(draft.draft_id)
    with pytest.raises(NotFound):  # SPIKE-005 D6: 초안 메시지 ID는 발송 후 404
        await mail.get_message(draft.message_id)


async def test_pagination_tokens_are_deterministic_and_validated(world: SimWorld) -> None:
    mail = connector(world)
    first = await mail.search_messages("", max_results=1)
    assert first.next_page_token == "o:1"
    for bad in ("x", "o:", "o:-1", "o:a"):
        with pytest.raises(InvalidQuery):
            await mail.search_messages("", page_token=bad)
    with pytest.raises(InvalidQuery):
        await mail.search_messages("", max_results=0)


async def test_calls_are_recorded_by_tool_name(world: SimWorld) -> None:
    rt = SimRuntime()
    mail = SimMailConnector(world, "acct-alice", rt)
    await mail.search_messages("in:inbox")
    draft = await mail.create_draft(spec())
    await mail.send_draft(draft.draft_id)
    assert [r.tool for r in rt.calls.records] == ["search_messages", "create_draft", "send_draft"]


# ---------------------------------------------------------------- FaultPlan (SPIKE-005 대조 절차)


async def test_result_unknown_send_is_applied_and_reconcilable(world: SimWorld) -> None:
    mail = connector(world, {"rules": [{"tool": "send_draft", "error": "result_unknown"}]})
    draft = await mail.create_draft(spec())
    with pytest.raises(ResultUnknown):
        await mail.send_draft(draft.draft_id)
    # 대조: 초안이 없고 스레드에서 실행 ID가 일치하는 SENT 메일이 있다.
    with pytest.raises(NotFound):
        await mail.get_draft(draft.draft_id)
    thread = await mail.get_thread(draft.thread_id)
    found = [
        m
        for m in thread.messages
        if "SENT" in m.labels and m.headers.get(ORBIT_EXECUTION_HEADER) == "exec-1"
    ]
    assert len(found) == 1


async def test_result_unknown_not_applied_keeps_draft_and_allows_resend(world: SimWorld) -> None:
    mail = connector(
        world,
        {"rules": [{"tool": "send_draft", "nth_call": 1, "error": "result_unknown_not_applied"}]},
    )
    draft = await mail.create_draft(spec())
    with pytest.raises(ResultUnknown):
        await mail.send_draft(draft.draft_id)
    assert (await mail.get_draft(draft.draft_id)).draft_id == draft.draft_id
    assert world.account("acct-alice").draft_log[-1].kind == "created"  # 발송 기록 없음
    sent = await mail.send_draft(draft.draft_id)
    assert sent.thread_id == draft.thread_id


# ---------------------------------------------------------------- 결정성


async def _scenario(world: SimWorld) -> list[str]:
    mail = connector(world)
    d1 = await mail.create_draft(spec())
    d2 = await mail.create_draft(spec(reply_to_message_id="msg-a-003"))
    sent = await mail.send_draft(d1.draft_id)
    return [d1.draft_id, d1.message_id, d1.thread_id, d2.draft_id, sent.id]


async def test_same_snapshot_seed_and_calls_give_same_ids(world: SimWorld) -> None:
    a = await _scenario(world.fork(7))
    b = await _scenario(world.fork(7))
    c = await _scenario(world.fork(8))
    assert a == b
    assert a != c


# ---------------------------------------------------------------- diff


async def test_diff_sees_existing_draft_sent(world: SimWorld) -> None:
    before = world.copy()
    mail = connector(world)
    await mail.send_draft("drf-a-001")
    d = diff(before, world)
    assert len(d.drafts_sent) == 1
    assert d.drafts_sent[0].message["labels"] == ["SENT"]
    assert not d.drafts_deleted
    assert not d.messages_added


async def test_diff_in_episode_create_then_send(world: SimWorld) -> None:
    before = world.copy()
    mail = connector(world)
    draft = await mail.create_draft(spec())
    await mail.send_draft(draft.draft_id)
    d = diff(before, world)
    assert len(d.drafts_created) == 1
    assert len(d.drafts_sent) == 1
    assert d.drafts_sent[0].message["subject"] == "제목"
    assert not d.drafts_deleted
    assert not d.messages_added  # 발송 메시지는 중복으로 들어가지 않는다


async def test_diff_in_episode_create_then_delete(world: SimWorld) -> None:
    before = world.copy()
    mail = connector(world)
    draft = await mail.create_draft(spec())
    await mail.delete_draft(draft.draft_id)
    d = diff(before, world)
    assert len(d.drafts_created) == 1
    assert len(d.drafts_deleted) == 1
    assert not d.drafts_sent


@pytest.mark.parametrize("seed", range(12))
async def test_diff_pairs_exactly_when_two_drafts_share_thread(world: SimWorld, seed: int) -> None:
    ep = world.fork(seed)
    before = ep.copy()
    mail = connector(ep)
    a = await mail.create_draft(
        spec(subject="A 제목", body_text="A 본문", reply_to_message_id="msg-a-003")
    )
    b = await mail.create_draft(
        spec(subject="B 제목", body_text="B 본문", reply_to_message_id="msg-a-003")
    )
    assert a.thread_id == b.thread_id
    # B를 먼저 만들고 A만 보낸다. 시드에 따라 ID 정렬 순서가 달라진다.
    await mail.send_draft(a.draft_id)

    d = diff(before, ep)
    assert len(d.drafts_created) == 2
    assert len(d.drafts_sent) == 1
    assert d.drafts_sent[0].draft["spec"]["subject"] == "A 제목"
    assert d.drafts_sent[0].message["subject"] == "A 제목"
    assert not d.drafts_deleted


@pytest.mark.parametrize("seed", range(12))
async def test_diff_pairs_exactly_for_preexisting_drafts_in_same_thread(
    world: SimWorld, seed: int
) -> None:
    ep = world.fork(seed)
    mail = connector(ep)
    extra = await mail.create_draft(
        spec(subject="Z 제목", body_text="Z 본문", reply_to_message_id="msg-a-001")
    )
    before = ep.copy()  # drf-a-001과 extra가 같은 thr-a-001 초안
    assert extra.thread_id == "thr-a-001"
    await mail.send_draft(extra.draft_id)
    d = diff(before, ep)
    assert len(d.drafts_sent) == 1
    assert d.drafts_sent[0].draft["spec"]["subject"] == "Z 제목"
    assert not d.drafts_deleted
    assert not d.drafts_created


async def test_diff_delete_plus_unrelated_sent_message_is_not_a_send(world: SimWorld) -> None:
    before = world.copy()
    mail = connector(world)
    await mail.delete_draft("drf-a-001")  # thr-a-001 초안 삭제
    other = await mail.create_draft(
        spec(reply_to_message_id="msg-a-001")
    )  # 같은 스레드의 다른 초안
    await mail.send_draft(other.draft_id)
    d = diff(before, world)
    assert len(d.drafts_deleted) == 1
    assert d.drafts_deleted[0].draft["spec"]["body_text"] == "초안 합성 본문입니다."
    assert len(d.drafts_sent) == 1
    assert d.drafts_sent[0].message["body_text"] == "본문"


async def test_diff_fallback_inference_compares_content_for_multiple_candidates(
    world: SimWorld,
) -> None:
    """기록 없이 상태를 직접 바꾼 경우, 같은 스레드 후보가 여럿이면 제목·본문으로 짝짓는다."""
    before = world.copy()
    mail = connector(world)
    decoy = await mail.create_draft(spec(subject="다른 제목", reply_to_message_id="msg-a-001"))
    await mail.send_draft(decoy.draft_id)
    same = await mail.create_draft(
        spec(
            subject="Re: 프로젝트 일정 문의",
            body_text="초안 합성 본문입니다.",
            reply_to_message_id="msg-a-001",
        )
    )
    sent = await mail.send_draft(same.draft_id)
    alice = world.account("acct-alice")
    alice.draft_log.clear()  # 기록 없는 월드 흉내
    del alice.drafts["drf-a-001"]
    d = diff(before, world)
    assert len(d.drafts_sent) == 1
    assert d.drafts_sent[0].message["body_text"] == sent.body_text
    assert d.drafts_sent[0].message["subject"] == "Re: 프로젝트 일정 문의"
    assert not d.drafts_deleted
    assert len(d.messages_added) == 1
    assert d.messages_added[0].message["subject"] == "다른 제목"


async def test_draft_log_survives_copy_fork_and_snapshot(world: SimWorld) -> None:
    mail = connector(world)
    draft = await mail.create_draft(spec())
    await mail.send_draft(draft.draft_id)
    alice = world.account("acct-alice")
    assert [e.kind for e in alice.draft_log] == ["created", "sent"]
    assert [e.seq for e in alice.draft_log] == [1, 2]
    assert alice.draft_log[1].sent_message_id in alice.messages

    for clone in (world.copy(), world.fork(99)):
        assert [e.kind for e in clone.account("acct-alice").draft_log] == ["created", "sent"]
    reloaded = SimWorld.load(world.to_dict(), world.clock)
    assert reloaded.account("acct-alice").draft_log == alice.draft_log
    assert reloaded.to_dict() == world.to_dict()


def test_snapshot_without_draft_log_loads_empty(world: SimWorld) -> None:
    assert world.account("acct-alice").draft_log == []
    assert "draft_log" not in world.to_dict()["accounts"]["acct-alice"]


# ---------------------------------------------------------------- 계약에서 옮긴 Sim 전용 동작


async def _ids(mail: SimMailConnector, query: str) -> list[str]:
    return [m.id for m in (await mail.search_messages(query, max_results=50)).messages]


async def test_fixture_exact_results_and_substring_matching(world: SimWorld) -> None:
    mail = connector(world)
    assert await _ids(mail, "") == ["msg-a-003", "msg-a-002", "msg-a-001"]
    assert await _ids(mail, "FROM:ALICE") == ["msg-a-002"]  # 이름 부분 문자열
    assert await _ids(mail, "from:bob in:inbox has:attachment") == ["msg-a-003"]
    assert await _ids(mail, '"다음 주 일정"') == ["msg-a-001"]  # 조사 결합(일정을)도 일치
    assert await _ids(mail, '"주 다음 일정"') == []
    assert await _ids(mail, "alice@example.com") == ["msg-a-002"]  # 키워드는 발신자도 본다
    assert await _ids(mail, "분기") == ["msg-a-003"]
    assert await _ids(mail, 'subject:"일정문의"') == []


async def test_pagination_exact_pages(world: SimWorld) -> None:
    mail = connector(world)
    first = await mail.search_messages("", max_results=2)
    assert [m.id for m in first.messages] == ["msg-a-003", "msg-a-002"]
    second = await mail.search_messages("", max_results=2, page_token=first.next_page_token)
    assert [m.id for m in second.messages] == ["msg-a-001"]
    assert second.next_page_token is None
    assert (await mail.search_messages("", max_results=3)).next_page_token is None


async def test_page_token_rejects_non_ascii_digits(world: SimWorld) -> None:
    mail = connector(world)
    for bad in ("o:²", "o:١", "o:１"):
        with pytest.raises(InvalidQuery):
            await mail.search_messages("", page_token=bad)


async def test_drafts_only_appear_in_drafts_search(world: SimWorld) -> None:
    mail = connector(world)
    draft = await mail.create_draft(spec(subject="유일한초안제목"))
    assert await _ids(mail, "유일한초안제목") == []
    assert await _ids(mail, "in:drafts 유일한초안제목") == [draft.message_id]
    assert draft.message_id not in await _ids(mail, "")
    drafts = await mail.search_messages("in:drafts")
    assert "msg-a-d01" in [m.id for m in drafts.messages]


async def test_get_thread_excludes_draft_messages(world: SimWorld) -> None:
    mail = connector(world)
    draft = await mail.create_draft(spec(reply_to_message_id="msg-a-003"))
    thread = await mail.get_thread("thr-a-002")
    assert [m.id for m in thread.messages] == ["msg-a-003"]
    assert draft.message_id not in {m.id for m in thread.messages}
    with pytest.raises(NotFound):  # 초안만 있는 새 스레드는 조회되지 않는다
        await mail.get_thread((await mail.create_draft(spec())).thread_id)


async def test_reply_to_missing_message_raises_not_found(world: SimWorld) -> None:
    with pytest.raises(NotFound):
        await connector(world).create_draft(spec(reply_to_message_id="msg-does-not-exist"))


async def test_send_does_not_deliver_to_other_account(world: SimWorld) -> None:
    alice = connector(world)
    bob = SimMailConnector(world, "acct-bob", SimRuntime())
    before = await _ids(bob, "")
    draft = await alice.create_draft(spec(subject="앨리스만보는제목"))
    await alice.send_draft(draft.draft_id)
    assert await _ids(bob, "") == before  # v0 단순화: 다른 계정으로 배달하지 않는다


async def test_other_accounts_fixture_ids_are_not_found(world: SimWorld) -> None:
    bob = SimMailConnector(world, "acct-bob", SimRuntime())
    with pytest.raises(NotFound):
        await bob.get_draft("drf-a-001")
    with pytest.raises(NotFound):
        await connector(world).get_message("msg-b-001")


async def test_resend_after_not_applied_leaves_exactly_one_sent_mail(world: SimWorld) -> None:
    mail = connector(
        world,
        {"rules": [{"tool": "send_draft", "nth_call": 1, "error": "result_unknown_not_applied"}]},
    )
    draft = await mail.create_draft(spec())
    with pytest.raises(ResultUnknown):
        await mail.send_draft(draft.draft_id)
    await mail.send_draft(draft.draft_id)
    sent = (await mail.search_messages("in:sent", max_results=50)).messages
    full = [await mail.get_message(m.id) for m in sent]
    assert [m.headers.get(ORBIT_EXECUTION_HEADER) for m in full].count("exec-1") == 1
    thread = await mail.get_thread(draft.thread_id)
    assert len(thread.messages) == 1


async def test_diff_fallback_single_candidate_with_different_content_is_not_a_send(
    world: SimWorld,
) -> None:
    before = world.copy()
    mail = connector(world)
    other = await mail.create_draft(spec(subject="다른 제목", reply_to_message_id="msg-a-001"))
    await mail.send_draft(other.draft_id)
    alice = world.account("acct-alice")
    alice.draft_log.clear()
    del alice.drafts["drf-a-001"]
    d = diff(before, world)
    assert not d.drafts_sent
    assert len(d.drafts_deleted) == 1
    assert len(d.messages_added) == 1


async def test_snapshot_rejects_sent_log_entry_without_message(world: SimWorld) -> None:
    mail = connector(world)
    draft = await mail.create_draft(spec())
    await mail.send_draft(draft.draft_id)
    data = world.to_dict()
    log = data["accounts"]["acct-alice"]["draft_log"]
    log[-1]["sent_message_id"] = "msg-ghost"
    with pytest.raises(SnapshotError):
        SimWorld.load(data, world.clock)


async def test_returned_messages_do_not_alias_world_headers(world: SimWorld) -> None:
    mail = connector(world)
    alice = world.account("acct-alice")
    draft = await mail.create_draft(spec())
    sent = await mail.send_draft(draft.draft_id)
    sent.headers["X-Tamper"] = "1"
    assert alice.messages[sent.id].headers == {ORBIT_EXECUTION_HEADER: "exec-1"}

    got = await mail.get_message(sent.id)
    got.headers["X-Tamper"] = "2"
    assert "X-Tamper" not in alice.messages[sent.id].headers

    thread = await mail.get_thread(sent.thread_id)
    for msg in thread.messages:
        msg.headers["X-Tamper"] = "3"
    assert all("X-Tamper" not in m.headers for m in alice.messages.values())

    before = world.to_dict()
    (await mail.get_message("msg-a-001")).headers["X-Tamper"] = "4"
    assert world.to_dict() == before
