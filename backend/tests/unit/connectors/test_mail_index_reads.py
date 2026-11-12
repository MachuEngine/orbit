"""색인용 읽기(ADR-0019 §2)의 구현별 동작: Sim 이력·만료 주입, Gmail 어댑터의 이력·404 변환.

계약 공통 동작은 `tests/contract`가 검증한다. 데이터는 모두 합성이다.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from orbit.connectors.contracts import (
    Address,
    HistoryExpired,
    InvalidQuery,
    MailMessage,
    PermissionRevoked,
)
from orbit.connectors.errors import connector_error_kind
from orbit.connectors.google.gmail import GmailConnector
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.mail import SimMailConnector
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import SimWorld

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from contract.fake_gmail import FakeGmail, make_transport  # noqa: E402

LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
ALICE = "acct-alice"


class _Tokens:
    def __init__(self, token: str) -> None:
        self._token = token

    async def access_token(self) -> str:
        return self._token

    async def refresh(self) -> str:
        raise PermissionRevoked("고정 토큰")


def _message(message_id: str, *, labels: tuple[str, ...], day: int) -> MailMessage:
    return MailMessage(
        id=message_id,
        thread_id=f"thr-{message_id}",
        from_=Address(email="bob@example.org"),
        to=(Address(email="alice@example.com"),),
        subject=f"합성 제목 {message_id}",
        body_text="합성 본문",
        sent_at=datetime(2026, 11, day, 9, 0, tzinfo=UTC),
        labels=labels,
    )


def _sim(world: SimWorld, *, page_size: int = 100) -> SimMailConnector:
    return SimMailConnector(world, ALICE, SimRuntime(), page_size=page_size)


async def test_sim_history_records_add_delete_and_label_changes(world: SimWorld) -> None:
    state = world.account(ALICE)
    mail = _sim(world)
    start = await mail.current_history_id()
    state.add_message(_message("new-1", labels=("INBOX",), day=7))
    state.set_labels("new-1", add=("Label_9",), remove=("INBOX",))
    state.delete_message("msg-a-003")

    page = await mail.list_history(start)
    assert page.added_ids == ("new-1",)
    assert page.labels_added_ids == ("new-1",)
    assert page.labels_removed_ids == ("new-1",)
    assert page.deleted_ids == ("msg-a-003",)
    assert page.history_id == "4"
    assert (await mail.list_history(page.history_id)).added_ids == ()
    # 삭제한 메시지는 라벨 조회에서도 빠진다.
    ids = (await mail.list_message_ids("INBOX", after=LONG_AGO)).message_ids
    assert "msg-a-003" not in ids and "new-1" not in ids


async def test_sim_set_labels_ignores_no_op_changes(world: SimWorld) -> None:
    state = world.account(ALICE)
    state.set_labels("msg-a-001", add=("INBOX",), remove=("Nothing",))
    assert state.history == []


async def test_sim_history_expiry_is_injectable(world: SimWorld) -> None:
    state = world.account(ALICE)
    mail = _sim(world)
    old = await mail.current_history_id()
    state.add_message(_message("new-1", labels=("INBOX",), day=7))
    state.expire_history()
    with pytest.raises(HistoryExpired):
        await mail.list_history(old)
    # 만료 뒤의 새 위치에서는 다시 읽을 수 있다.
    assert (await mail.list_history(await mail.current_history_id())).added_ids == ()
    for bad in ("", "abc", "999"):
        with pytest.raises(HistoryExpired):
            await mail.list_history(bad)
    assert connector_error_kind(HistoryExpired()) == "history_expired"


async def test_sim_pages_ids_and_history_deterministically(world: SimWorld) -> None:
    state = world.account(ALICE)
    mail = _sim(world, page_size=2)
    for i in range(5):
        state.add_message(_message(f"p-{i}", labels=("INBOX",), day=10 + i))
    seen: list[str] = []
    token = None
    while True:
        page = await mail.list_message_ids("INBOX", after=LONG_AGO, page_token=token)
        assert len(page.message_ids) <= 2
        seen += page.message_ids
        token = page.next_page_token
        if token is None:
            break
    assert seen[:5] == ["p-4", "p-3", "p-2", "p-1", "p-0"]  # 최근순
    assert len(seen) == 7
    added: list[str] = []
    token = None
    while True:
        history = await mail.list_history("0", page_token=token)
        added += history.added_ids
        token = history.next_page_token
        if token is None:
            break
    assert added == [f"p-{i}" for i in range(5)]
    with pytest.raises(InvalidQuery):
        await mail.list_message_ids("INBOX", after=LONG_AGO, page_token="x")


async def test_sim_labels_include_user_labels(world: SimWorld) -> None:
    state = world.account(ALICE)
    state.user_labels["Label_1"] = "프로젝트"
    state.add_message(_message("n", labels=("INBOX", "Label_2"), day=8))
    labels = {label.id: label for label in await _sim(world).list_labels()}
    assert labels["Label_1"].name == "프로젝트" and labels["Label_1"].kind == "user"
    assert labels["Label_2"].kind == "user" and labels["INBOX"].kind == "system"


async def test_sim_fault_plan_can_target_index_reads(world: SimWorld) -> None:
    plan = FaultPlan.from_dict({"rules": [{"tool": "list_history", "error": "transient"}]})
    mail = SimMailConnector(world, ALICE, SimRuntime(plan))
    from orbit.connectors.contracts import TransientError

    with pytest.raises(TransientError):
        await mail.list_history("0")


# ------------------------------------------------------------------ Gmail 어댑터


def _gmail(world: SimWorld) -> tuple[GmailConnector, FakeGmail]:
    fake = FakeGmail(world.account(ALICE), now=datetime(2026, 11, 11, 9, 0, tzinfo=UTC))
    client = httpx.AsyncClient(
        base_url="https://gmail.mock/gmail/v1", transport=make_transport(fake)
    )
    mail = GmailConnector(_Tokens(fake.valid_token), client=client, allow_test_base_url=True)
    return mail, fake


async def test_gmail_labels_map_system_and_user(world: SimWorld) -> None:
    mail, _ = _gmail(world)
    labels = {label.id: label for label in await mail.list_labels()}
    assert labels["INBOX"].kind == "system"
    assert labels["Label_1"].kind == "user" and labels["Label_1"].name == "프로젝트"


async def test_gmail_history_collects_all_change_kinds_and_expires(world: SimWorld) -> None:
    mail, fake = _gmail(world)
    start = await mail.current_history_id()
    assert start == str(fake.history_seq)
    fake.change_labels("msg-a-001", add=["Label_1"], remove=["INBOX"])
    fake.remove_message("msg-a-003")
    page = await mail.list_history(start)
    assert page.labels_added_ids == ("msg-a-001",)
    assert page.labels_removed_ids == ("msg-a-001",)
    assert page.deleted_ids == ("msg-a-003",)
    assert page.history_id == str(fake.history_seq)
    # 라벨 ID 조회는 서버가 걸러 준다.
    ids = (await mail.list_message_ids("Label_1", after=LONG_AGO)).message_ids
    assert ids == ("msg-a-001",)

    fake.expire_history()
    with pytest.raises(HistoryExpired):
        await mail.list_history(start)


async def test_gmail_pages_through_next_page_token(world: SimWorld) -> None:
    mail, fake = _gmail(world)
    start = await mail.current_history_id()
    for i in range(3):
        fake.change_labels("msg-a-001", add=[f"L{i}"], remove=[])
    fake.history_page_size = 2
    first = await mail.list_history(start)
    assert first.next_page_token is not None
    second = await mail.list_history(start, page_token=first.next_page_token)
    assert second.next_page_token is None
    assert first.labels_added_ids == ("msg-a-001",)
    assert second.history_id == first.history_id


async def test_gmail_list_message_ids_sends_label_and_epoch_after(world: SimWorld) -> None:
    mail, fake = _gmail(world)
    after = datetime(2026, 11, 6, tzinfo=UTC)
    page = await mail.list_message_ids("INBOX", after=after)
    assert len(page.message_ids) == 1
    sent = [r for r in fake.requests if r[1].endswith("/messages")]
    assert sent  # 목록 호출만 했고 본문은 읽지 않았다
    assert not any("/messages/" in path for _, path in fake.requests)
