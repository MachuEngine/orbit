import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orbit.actions.executor import ApprovalRejected, execute_approved
from orbit.actions.models import CancelEvent, CreateEvent, SendMail, UpdateEventTime, WriteAction
from orbit.actions.store import (
    FAILED,
    NEEDS_USER,
    PENDING,
    SUCCEEDED,
    UNKNOWN,
    ExecutionRecord,
    InMemoryExecutionStore,
)
from orbit.connectors.contracts import Address, Attendee, DraftSpec, EventDraft, EventPatch

from .conftest import ACCOUNT, CALENDAR, NOW, Env, faults

MakeEnv = Callable[..., Env]

T0 = datetime(2026, 11, 20, 5, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
EVENT_ID = "evalice001"
DEAD = uuid.UUID(int=999)  # 종료된 실행자

CREATE = CreateEvent(
    calendar_id=CALENDAR,
    title="합성 회의",
    start=T0,
    end=T1,
    attendees=("bob@example.org",),
    description="합성 설명",
)
UPDATE = UpdateEventTime(calendar_id=CALENDAR, event_id=EVENT_ID, start=T0, end=T1)
CANCEL = CancelEvent(calendar_id=CALENDAR, event_id=EVENT_ID)
MAIL = SendMail(to=("bob@example.org",), cc=(), subject="합성 제목", body="합성 본문")
REPLY = SendMail(
    to=("bob@example.org",), subject="Re: 합성", body="합성 답장", reply_to_thread_id="thr-a-001"
)
ACTIONS: dict[str, WriteAction] = {
    "create": CREATE,
    "update": UPDATE,
    "cancel": CANCEL,
    "mail": MAIL,
    "reply": REPLY,
}


async def run(env: Env, action: WriteAction, **overrides: Any) -> ExecutionRecord:
    return await execute_approved(env.approval(action, **overrides), action, **env.kwargs())


# ---------------------------------------------------------------- 승인 검사


@pytest.mark.parametrize("name", list(ACTIONS))
@pytest.mark.parametrize("status", ["pending", "rejected", "expired"])
async def test_not_approved_makes_no_call(make_env: MakeEnv, name: str, status: str) -> None:
    env = make_env()
    with pytest.raises(ApprovalRejected):
        await run(env, ACTIONS[name], status=status)
    assert env.conns.calls.records == []
    assert await env.store.get(uuid.UUID(int=1)) is None
    assert env.diff().is_empty


@pytest.mark.parametrize("name", list(ACTIONS))
async def test_expired_by_clock_makes_no_call(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    with pytest.raises(ApprovalRejected) as info:
        await run(env, ACTIONS[name], expires_at=NOW)  # 만료 시각과 같으면 만료다
    assert info.value.reason == "expired"
    assert env.conns.calls.records == []
    assert await env.store.get(uuid.UUID(int=1)) is None


@pytest.mark.parametrize("name", list(ACTIONS))
async def test_hash_mismatch_makes_no_call(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    with pytest.raises(ApprovalRejected) as info:
        await run(env, ACTIONS[name], args_hash="0" * 64)
    assert info.value.reason == "hash_mismatch"
    assert env.conns.calls.records == []
    assert await env.store.get(uuid.UUID(int=1)) is None


async def test_changed_action_after_approval_is_rejected(make_env: MakeEnv) -> None:
    env = make_env()
    approval = env.approval(MAIL)
    tampered = MAIL.model_copy(update={"to": ("evil@example.net",)})
    with pytest.raises(ApprovalRejected):
        await execute_approved(approval, tampered, **env.kwargs())
    assert env.conns.calls.records == []


# ---------------------------------------------------------------- 정상 실행


async def test_create_event_succeeds_with_execution_id(make_env: MakeEnv) -> None:
    env = make_env()
    record = await run(env, CREATE)
    assert record.status == SUCCEEDED and record.error_kind is None
    assert record.external_ref["event_id"] == record.id.hex
    created = env.diff().events_created
    assert len(created) == 1 and created[0].event["id"] == record.id.hex
    assert env.calls("create_event") == ["create_event"]


async def test_update_event_succeeds_and_records_before_version(make_env: MakeEnv) -> None:
    env = make_env()
    record = await run(env, UPDATE)
    assert record.status == SUCCEEDED
    assert record.external_ref["before_version"] == "v-e1"
    assert len(env.diff().events_updated) == 1
    event = await env.calendar.get_event(CALENDAR, EVENT_ID)
    assert (event.start, event.end) == (T0, T1)
    assert record.external_ref["version"] == event.version


async def test_cancel_event_succeeds(make_env: MakeEnv) -> None:
    env = make_env()
    record = await run(env, CANCEL)
    assert record.status == SUCCEEDED
    assert len(env.diff().events_cancelled) == 1


async def test_send_mail_uses_draft_with_header_and_records_refs(make_env: MakeEnv) -> None:
    env = make_env()
    record = await run(env, MAIL)
    assert record.status == SUCCEEDED
    assert env.calls("create_draft", "send_draft") == ["create_draft", "send_draft"]
    assert record.external_ref["draft_id"] and record.external_ref["thread_id"]
    assert env.sent_with_header(record.id.hex) == 1
    assert len(env.diff().drafts_sent) == 1


async def test_reply_goes_to_thread(make_env: MakeEnv) -> None:
    env = make_env()
    record = await run(env, REPLY)
    assert record.status == SUCCEEDED
    assert record.external_ref["thread_id"] == "thr-a-001"
    thread = await env.mail.get_thread("thr-a-001")
    assert sum(1 for m in thread.messages if "SENT" in m.labels and m.headers) == 1


async def test_draft_ref_is_recorded_before_send(make_env: MakeEnv) -> None:
    """초안 ID는 발송 호출 전에 커밋돼야 한다(발송 호출이 오류로 끝나도 남는다)."""
    env = make_env(faults({"tool": "send_draft", "error": "permission_revoked"}))
    record = await run(env, MAIL)
    assert record.status == FAILED and record.error_kind == "PermissionRevoked"
    stored = await env.store.get(uuid.UUID(int=1))
    assert stored is not None and stored.external_ref["draft_id"]


# ---------------------------------------------------------------- 중복 실행


@pytest.mark.parametrize("name", list(ACTIONS))
async def test_same_approval_twice_sequential_writes_once(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    first = await run(env, ACTIONS[name])
    calls_after_first = len(env.conns.calls.records)
    second = await run(env, ACTIONS[name])
    assert first == second and first.status == SUCCEEDED
    assert len(env.conns.calls.records) == calls_after_first  # 두 번째는 외부 호출이 없다
    d = env.diff()
    assert (
        len(d.events_created) + len(d.events_updated) + len(d.events_cancelled) + len(d.drafts_sent)
        == 1
    )


class YieldingStore(InMemoryExecutionStore):
    """모든 저장소 호출에서 제어를 넘겨 동시 실행이 실제로 끼어들게 한다."""

    async def begin(
        self, approval_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float
    ) -> tuple[ExecutionRecord, bool]:
        await asyncio.sleep(0)
        result = await super().begin(approval_id, owner, lease_seconds)
        await asyncio.sleep(0)
        return result

    async def claim(self, execution_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float) -> bool:
        await asyncio.sleep(0)
        result = await super().claim(execution_id, owner, lease_seconds)
        await asyncio.sleep(0)
        return result

    async def get(self, approval_id: uuid.UUID) -> ExecutionRecord | None:
        await asyncio.sleep(0)
        return await super().get(approval_id)

    async def record_ref(
        self, execution_id: uuid.UUID, owner: uuid.UUID, external_ref: dict[str, Any]
    ) -> bool:
        await asyncio.sleep(0)
        result = await super().record_ref(execution_id, owner, external_ref)
        await asyncio.sleep(0)
        return result

    async def finish(
        self,
        execution_id: uuid.UUID,
        owner: uuid.UUID,
        status: str,
        external_ref: dict[str, Any],
        error_kind: str | None,
    ) -> bool:
        await asyncio.sleep(0)
        result = await super().finish(execution_id, owner, status, external_ref, error_kind)
        await asyncio.sleep(0)
        return result


def writes(d: Any) -> int:
    return (
        len(d.events_created) + len(d.events_updated) + len(d.events_cancelled) + len(d.drafts_sent)
    )


@pytest.mark.parametrize("name", list(ACTIONS))
async def test_concurrent_executors_new_row_write_once(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    env.store = YieldingStore(env.clock)
    results = await asyncio.gather(*(run(env, ACTIONS[name]) for _ in range(4)))
    assert len({r.id for r in results}) == 1
    final = await env.store.get(uuid.UUID(int=1))
    assert final is not None and final.status == SUCCEEDED
    assert writes(env.diff()) == 1
    assert len(env.calls("create_event", "update_event", "cancel_event", "send_draft")) == 1


@pytest.mark.parametrize("name", list(ACTIONS))
async def test_two_owners_on_same_pending_row_write_once(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    env.store = YieldingStore(env.clock)
    await seed_pending(env, ACTIONS[name])
    await asyncio.gather(run(env, ACTIONS[name]), run(env, ACTIONS[name]))
    final = await env.store.get(uuid.UUID(int=1))
    assert final is not None and final.status == SUCCEEDED
    assert writes(env.diff()) == 1


async def test_active_lease_blocks_other_executor_until_expiry(make_env: MakeEnv) -> None:
    env = make_env()
    record, _ = await env.store.begin(uuid.UUID(int=1), DEAD, 300.0)  # 다른 실행자가 소유 중
    blocked = await run(env, MAIL)
    assert blocked.id == record.id and blocked.status == PENDING
    assert env.conns.calls.records == []
    env.clock.advance(timedelta(minutes=5))  # lease 만료: 이어받는다
    taken = await run(env, MAIL)
    assert taken.status == SUCCEEDED and taken.lease_owner is not None
    assert taken.lease_owner != DEAD
    assert env.sent_with_header(record.id.hex) == 1


@pytest.mark.parametrize("name", list(ACTIONS))
async def test_lost_ownership_stops_external_calls(make_env: MakeEnv, name: str) -> None:
    """record_ref가 거절되면(소유권 상실) 외부 호출 없이 현재 기록을 돌려준다."""
    env = make_env()

    class Stealing(InMemoryExecutionStore):
        async def record_ref(
            self, execution_id: uuid.UUID, owner: uuid.UUID, external_ref: dict[str, Any]
        ) -> bool:
            self._update(execution_id, lease_owner=DEAD)  # 다른 실행자가 가져갔다
            return await super().record_ref(execution_id, owner, external_ref)

    env.store = Stealing(env.clock)
    record = await run(env, ACTIONS[name])
    assert record.status == PENDING and record.lease_owner == DEAD
    assert env.calls("create_event", "update_event", "cancel_event", "send_draft") == []
    assert env.calls("create_draft") == []


# ---------------------------------------------------------------- 결과 불명 대조

UNKNOWN_ERRORS = ["result_unknown", "result_unknown_not_applied"]


@pytest.mark.parametrize("error", UNKNOWN_ERRORS)
async def test_create_event_result_unknown(make_env: MakeEnv, error: str) -> None:
    env = make_env(faults({"tool": "create_event", "nth_call": 1, "error": error}))
    record = await run(env, CREATE)
    assert record.status == SUCCEEDED
    assert len(env.diff().events_created) == 1
    events = await env.calendar.list_events(CALENDAR, T0, T1)
    assert [e.id for e in events] == [record.id.hex]
    # 적용 후 오류는 재호출 없이 조회로 확인하고, 미적용 오류는 같은 ID로 1회 재호출한다.
    expected = 1 if error == "result_unknown" else 2
    assert len(env.calls("create_event")) == expected


@pytest.mark.parametrize("error", UNKNOWN_ERRORS)
@pytest.mark.parametrize("name", ["update", "cancel"])
async def test_modify_event_result_unknown(make_env: MakeEnv, name: str, error: str) -> None:
    tool = "update_event" if name == "update" else "cancel_event"
    env = make_env(faults({"tool": tool, "nth_call": 1, "error": error}))
    record = await run(env, ACTIONS[name])
    assert record.status == SUCCEEDED
    d = env.diff()
    assert len(d.events_updated) + len(d.events_cancelled) == 1
    expected = 1 if error == "result_unknown" else 2
    assert len(env.calls(tool)) == expected
    current = await env.calendar.get_event(CALENDAR, EVENT_ID)
    assert record.external_ref["version"] == current.version


@pytest.mark.parametrize("error", UNKNOWN_ERRORS)
@pytest.mark.parametrize("name", ["mail", "reply"])
async def test_mail_send_draft_result_unknown(make_env: MakeEnv, name: str, error: str) -> None:
    env = make_env(faults({"tool": "send_draft", "nth_call": 1, "error": error}))
    record = await run(env, ACTIONS[name])
    assert record.status == SUCCEEDED
    assert env.sent_with_header(record.id.hex) == 1
    assert len(env.diff().drafts_sent) == 1
    assert len(env.calls("create_draft")) == 1
    # 적용 후 오류: 초안이 없어 스레드에서 보낸 메일을 찾는다. 미적용: 초안이 남아 1회 재발송.
    assert len(env.calls("send_draft")) == (1 if error == "result_unknown" else 2)
    assert record.external_ref["sent_message_id"]


@pytest.mark.parametrize("error", UNKNOWN_ERRORS)
@pytest.mark.parametrize("name", ["mail", "reply"])
async def test_mail_create_draft_result_unknown(make_env: MakeEnv, name: str, error: str) -> None:
    env = make_env(faults({"tool": "create_draft", "nth_call": 1, "error": error}))
    record = await run(env, ACTIONS[name])
    assert record.status == SUCCEEDED
    assert env.sent_with_header(record.id.hex) == 1
    assert len(env.diff().drafts_sent) == 1
    assert len(env.calls("send_draft")) == 1
    assert record.external_ref["orphan_draft_possible"] is True


async def test_mail_create_draft_unknown_twice_needs_user(make_env: MakeEnv) -> None:
    env = make_env(faults({"tool": "create_draft", "error": "result_unknown"}))
    record = await run(env, MAIL)
    assert record.status == NEEDS_USER
    assert env.calls("send_draft") == []
    assert len(env.calls("create_draft")) == 2


async def test_send_draft_resend_unknown_needs_user(make_env: MakeEnv) -> None:
    env = make_env(faults({"tool": "send_draft", "error": "result_unknown_not_applied"}))
    record = await run(env, MAIL)
    assert record.status == NEEDS_USER
    assert len(env.calls("send_draft")) == 2
    assert env.diff().drafts_sent == ()


async def test_create_event_recall_unknown_needs_user(make_env: MakeEnv) -> None:
    env = make_env(faults({"tool": "create_event", "error": "result_unknown_not_applied"}))
    record = await run(env, CREATE)
    assert record.status == NEEDS_USER
    assert len(env.calls("create_event")) == 2


# ---------------------------------------------------------------- 선기록 후 종료 흉내(재진입)


def mail_spec(execution_hex: str, **overrides: Any) -> DraftSpec:
    values: dict[str, Any] = {
        "to": (Address(email="bob@example.org"),),
        "subject": MAIL.subject,
        "body_text": MAIL.body,
        "execution_id": execution_hex,
    }
    values.update(overrides)
    return DraftSpec(**values)


async def seed_pending(env: Env, action: WriteAction) -> ExecutionRecord:
    """프로세스가 `begin` 직후 종료된 상태를 만든다."""
    record, created = await env.store.begin(uuid.UUID(int=1), DEAD, 0.0)
    assert created and record.status == PENDING
    return record


@pytest.mark.parametrize("name", list(ACTIONS))
async def test_reentry_with_pending_row_never_called(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    pending = await seed_pending(env, ACTIONS[name])
    record = await run(env, ACTIONS[name])
    assert record.id == pending.id and record.status == SUCCEEDED
    d = env.diff()
    assert (
        len(d.events_created) + len(d.events_updated) + len(d.events_cancelled) + len(d.drafts_sent)
        == 1
    )


async def test_reentry_create_event_already_applied(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, CREATE)
    await env.store.record_ref(pending.id, DEAD, {"attempted": True})
    await env.calendar.create_event(
        CALENDAR,
        pending.id.hex,
        EventDraft(
            title=CREATE.title,
            start=T0,
            end=T1,
            description="합성 설명",
            attendees=(Attendee(email="bob@example.org"),),
        ),
    )
    record = await run(env, CREATE)
    assert record.status == SUCCEEDED
    assert len(env.calls("create_event")) == 1  # 선행 호출만 있다
    assert len(env.diff().events_created) == 1


async def test_reentry_create_event_with_different_content_needs_user(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, CREATE)
    await env.store.record_ref(pending.id, DEAD, {"attempted": True})
    await env.calendar.create_event(
        CALENDAR, pending.id.hex, EventDraft(title="다른 제목", start=T0, end=T1)
    )
    record = await run(env, CREATE)
    assert record.status == NEEDS_USER


@pytest.mark.parametrize("name", ["update", "cancel"])
async def test_reentry_modify_before_call(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    pending = await seed_pending(env, ACTIONS[name])
    await env.store.record_ref(pending.id, DEAD, {"before_version": "v-e1"})  # 기록 후 호출 전 종료
    record = await run(env, ACTIONS[name])
    assert record.status == SUCCEEDED
    d = env.diff()
    assert len(d.events_updated) + len(d.events_cancelled) == 1


@pytest.mark.parametrize("name", ["update", "cancel"])
async def test_reentry_modify_after_call_applied(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    pending = await seed_pending(env, ACTIONS[name])
    await env.store.record_ref(pending.id, DEAD, {"before_version": "v-e1"})
    if name == "update":
        await env.calendar.update_event(
            CALENDAR, EVENT_ID, EventPatch(start=T0, end=T1), if_match="v-e1"
        )
    else:
        await env.calendar.cancel_event(CALENDAR, EVENT_ID, if_match="v-e1")
    record = await run(env, ACTIONS[name])
    assert record.status == SUCCEEDED
    assert len(env.calls("update_event", "cancel_event")) == 1  # 재호출 없음


async def test_reentry_mail_before_draft_recorded(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, MAIL)
    await env.store.record_ref(pending.id, DEAD, {"draft_attempted": True})
    record = await run(env, MAIL)
    assert record.status == SUCCEEDED
    assert env.sent_with_header(record.id.hex) == 1
    assert record.external_ref["orphan_draft_possible"] is True


async def test_reentry_mail_without_marker_is_fresh(make_env: MakeEnv) -> None:
    env = make_env()
    await seed_pending(env, MAIL)
    record = await run(env, MAIL)
    assert record.status == SUCCEEDED
    assert "orphan_draft_possible" not in record.external_ref
    assert record.external_ref["draft_attempted"] is True


async def test_reentry_mail_draft_created_not_sent(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, MAIL)
    draft = await env.mail.create_draft(mail_spec(pending.id.hex))
    await env.store.record_ref(
        pending.id, DEAD, {"draft_id": draft.draft_id, "thread_id": draft.thread_id}
    )
    record = await run(env, MAIL)
    assert record.status == SUCCEEDED
    assert len(env.calls("create_draft")) == 1  # 새 초안을 만들지 않는다
    assert len(env.calls("send_draft")) == 1


async def test_reentry_mail_already_sent(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, MAIL)
    draft = await env.mail.create_draft(mail_spec(pending.id.hex))
    await env.mail.send_draft(draft.draft_id)
    await env.store.record_ref(
        pending.id, DEAD, {"draft_id": draft.draft_id, "thread_id": draft.thread_id}
    )
    record = await run(env, MAIL)
    assert record.status == SUCCEEDED
    assert env.calls("send_draft") == ["send_draft"]  # 선행 호출뿐
    assert env.sent_with_header(pending.id.hex) == 1


async def test_reentry_after_expiry_does_not_recall(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, CANCEL)
    await env.store.record_ref(pending.id, DEAD, {"before_version": "v-e1"})
    env.clock.advance(timedelta(hours=1))
    record = await run(env, CANCEL)
    assert record.status == NEEDS_USER
    assert env.calls("cancel_event") == []


@pytest.mark.parametrize("name", list(ACTIONS))
@pytest.mark.parametrize("row", ["pending", "unknown"])
@pytest.mark.parametrize("why", ["expired", "rejected"])
async def test_reentry_without_marker_and_invalid_approval_makes_no_call(
    make_env: MakeEnv, name: str, row: str, why: str
) -> None:
    env = make_env()
    pending = await seed_pending(env, ACTIONS[name])
    if row == "unknown":
        assert await env.store.finish(pending.id, DEAD, UNKNOWN, {}, "TransientError")
    if why == "expired":
        env.clock.advance(timedelta(hours=1))
        record = await run(env, ACTIONS[name])
        assert record.error_kind == "expired"
    else:
        record = await run(env, ACTIONS[name], status="rejected")
        assert record.error_kind == "not_approved"
    assert record.status == FAILED
    assert env.conns.calls.records == []


WRITE_CALLS = ("create_event", "update_event", "cancel_event", "create_draft", "send_draft")
MARKERS: dict[str, dict[str, Any]] = {
    "create": {"attempted": True},
    "update": {"before_version": "v-e1"},
    "cancel": {"before_version": "v-e1"},
    "mail": {"draft_attempted": True},
}


@pytest.mark.parametrize("why", ["expired", "rejected"])
@pytest.mark.parametrize("name", list(MARKERS))
async def test_marker_then_invalid_approval_not_applied_needs_user_without_writes(
    make_env: MakeEnv, name: str, why: str
) -> None:
    env = make_env()
    pending = await seed_pending(env, ACTIONS[name])
    await env.store.record_ref(pending.id, DEAD, MARKERS[name])  # 표식 기록 직후 종료
    if why == "expired":
        env.clock.advance(timedelta(hours=1))
        record = await run(env, ACTIONS[name])
    else:
        record = await run(env, ACTIONS[name], status="rejected")
    assert record.status == NEEDS_USER
    assert env.calls(*WRITE_CALLS) == []
    assert env.diff().is_empty


@pytest.mark.parametrize("name", ["create", "update", "cancel"])
async def test_marker_then_expired_applied_is_succeeded_by_read(
    make_env: MakeEnv, name: str
) -> None:
    env = make_env()
    pending = await seed_pending(env, ACTIONS[name])
    await env.store.record_ref(pending.id, DEAD, MARKERS[name])
    if name == "create":
        await env.calendar.create_event(
            CALENDAR,
            pending.id.hex,
            EventDraft(
                title=CREATE.title,
                start=T0,
                end=T1,
                description="합성 설명",
                attendees=(Attendee(email="bob@example.org"),),
            ),
        )
    elif name == "update":
        await env.calendar.update_event(
            CALENDAR, EVENT_ID, EventPatch(start=T0, end=T1), if_match="v-e1"
        )
    else:
        await env.calendar.cancel_event(CALENDAR, EVENT_ID, if_match="v-e1")
    env.clock.advance(timedelta(hours=1))
    before_calls = env.calls(*WRITE_CALLS)
    record = await run(env, ACTIONS[name])
    assert record.status == SUCCEEDED
    assert env.calls(*WRITE_CALLS) == before_calls  # 재호출 없음


async def test_mail_expired_reentry_without_draft_id_creates_no_draft(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, MAIL)
    await env.store.record_ref(pending.id, DEAD, MARKERS["mail"])
    env.clock.advance(timedelta(hours=1))
    record = await run(env, MAIL)
    assert record.status == NEEDS_USER
    assert env.conns.calls.records == []


async def test_mail_expired_reentry_with_unsent_draft_does_not_send(make_env: MakeEnv) -> None:
    env = make_env()
    await _draft_for_resend(env)
    env.clock.advance(timedelta(hours=1))
    record = await run(env, MAIL)
    assert record.status == NEEDS_USER
    assert env.calls("send_draft", "create_draft") == ["create_draft"]  # 선행 호출뿐


async def test_mail_expired_reentry_with_sent_draft_succeeds_by_thread(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, MAIL)
    draft = await env.mail.create_draft(mail_spec(pending.id.hex))
    await env.mail.send_draft(draft.draft_id)
    await env.store.record_ref(
        pending.id, DEAD, {"draft_id": draft.draft_id, "thread_id": draft.thread_id}
    )
    env.clock.advance(timedelta(hours=1))
    record = await run(env, MAIL)
    assert record.status == SUCCEEDED
    assert env.calls("send_draft", "create_draft") == ["create_draft", "send_draft"]  # 선행만


async def test_reentry_is_still_checked_against_hash(make_env: MakeEnv) -> None:
    env = make_env()
    await seed_pending(env, CANCEL)
    with pytest.raises(ApprovalRejected):
        await run(env, CANCEL, args_hash="0" * 64)
    assert env.conns.calls.records == []


# ---------------------------------------------------------------- 실패·판단 불가


@pytest.mark.parametrize("name", ["update", "cancel"])
async def test_precondition_failed_is_failed_without_recall(make_env: MakeEnv, name: str) -> None:
    tool = "update_event" if name == "update" else "cancel_event"
    env = make_env(faults({"tool": tool, "nth_call": 1, "error": "precondition_failed"}))
    record = await run(env, ACTIONS[name])
    assert record.status == FAILED and record.error_kind == "PreconditionFailed"
    assert len(env.calls(tool)) == 1
    assert env.diff().is_empty


async def test_real_interleaved_change_is_failed(make_env: MakeEnv) -> None:
    """get_event 뒤에 다른 변경이 끼어들면 if_match가 어긋나 실패하고 재호출하지 않는다."""
    env = make_env()
    calendar = env.calendar

    class Interfering:
        def __getattr__(self, name: str) -> Any:
            return getattr(calendar, name)

        async def get_event(self, calendar_id: str, event_id: str) -> Any:
            event = await calendar.get_event(calendar_id, event_id)
            await calendar.update_event(
                calendar_id, event_id, EventPatch(title="끼어든 변경"), if_match=event.version
            )
            return event

    record = await execute_approved(
        env.approval(UPDATE),
        UPDATE,
        calendar=Interfering(),
        mail=env.mail,
        store=env.store,
        clock=env.clock,
    )
    assert record.status == FAILED and record.error_kind == "PreconditionFailed"
    assert env.calls("update_event").count("update_event") == 2  # 끼어든 변경 + 우리 호출 1번


@pytest.mark.parametrize("name", ["update", "cancel"])
async def test_reentry_with_unknown_other_change_needs_user(make_env: MakeEnv, name: str) -> None:
    env = make_env()
    pending = await seed_pending(env, ACTIONS[name])
    await env.store.record_ref(pending.id, DEAD, {"before_version": "v-e1"})
    await env.calendar.update_event(
        CALENDAR, EVENT_ID, EventPatch(title="다른 사용자의 변경"), if_match="v-e1"
    )
    record = await run(env, ACTIONS[name])
    assert record.status == NEEDS_USER
    assert len(env.calls("update_event", "cancel_event")) == 1  # 재호출하지 않았다


async def test_modify_missing_event_before_write_is_failed(make_env: MakeEnv) -> None:
    env = make_env()
    action = CancelEvent(calendar_id=CALENDAR, event_id="nonexistent1")
    record = await run(env, action)
    assert record.status == FAILED and record.error_kind == "NotFound"
    assert env.calls("cancel_event") == []


async def test_reentry_event_vanished_needs_user(make_env: MakeEnv) -> None:
    env = make_env()
    action = CancelEvent(calendar_id=CALENDAR, event_id="nonexistent1")
    pending = await seed_pending(env, action)
    await env.store.record_ref(pending.id, DEAD, {"before_version": "v-x"})
    record = await run(env, action)
    assert record.status == NEEDS_USER


async def test_mail_draft_gone_and_no_sent_message_needs_user(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, MAIL)
    await env.store.record_ref(pending.id, DEAD, {"draft_id": "drf-gone", "thread_id": "thr-a-002"})
    record = await run(env, MAIL)
    assert record.status == NEEDS_USER
    assert env.calls("send_draft") == []


async def test_mail_draft_and_thread_gone_needs_user(make_env: MakeEnv) -> None:
    env = make_env()
    pending = await seed_pending(env, MAIL)
    await env.store.record_ref(pending.id, DEAD, {"draft_id": "drf-gone", "thread_id": "thr-gone"})
    record = await run(env, MAIL)
    assert record.status == NEEDS_USER


async def test_mail_reply_to_missing_thread_failed_before_write(make_env: MakeEnv) -> None:
    env = make_env()
    action = SendMail(to=("bob@example.org",), subject="s", body="b", reply_to_thread_id="thr-no")
    record = await run(env, action)
    assert record.status == FAILED
    assert env.calls("create_draft") == []


@pytest.mark.parametrize(
    "error,kind",
    [
        ("transient", "TransientError"),
        ("rate_limited", "RateLimited"),
        ("permission_revoked", "PermissionRevoked"),
        ("not_found", "NotFound"),
    ],
)
async def test_known_not_applied_errors_are_failed_without_retry(
    make_env: MakeEnv, error: str, kind: str
) -> None:
    env = make_env(faults({"tool": "create_event", "error": error}))
    record = await run(env, CREATE)
    assert record.status == FAILED and record.error_kind == kind
    assert len(env.calls("create_event")) == 1
    again = await run(env, CREATE)
    assert again == record  # 종료된 행은 다시 실행하지 않는다
    assert len(env.calls("create_event")) == 1


async def test_reconcile_read_error_leaves_unknown_then_resumes(make_env: MakeEnv) -> None:
    env = make_env(
        faults(
            {"tool": "create_event", "nth_call": 1, "error": "result_unknown"},
            {"tool": "get_event", "nth_call": 1, "error": "transient"},
        )
    )
    first = await run(env, CREATE)
    assert first.status == UNKNOWN and first.error_kind == "TransientError"
    second = await run(env, CREATE)
    assert second.status == SUCCEEDED and second.id == first.id
    assert len(env.diff().events_created) == 1
    assert len(env.calls("create_event")) == 1


# ---------------------------------------------------------------- 결정성·기록 위생


async def scenario(make_env: MakeEnv) -> tuple[Any, ...]:
    env = make_env(
        faults(
            {"tool": "create_event", "nth_call": 1, "error": "result_unknown_not_applied"},
            {"tool": "send_draft", "nth_call": 1, "error": "result_unknown"},
        ),
        seed=5,
    )
    out: list[Any] = []
    actions: list[WriteAction] = [CREATE, UPDATE, MAIL]
    for i, action in enumerate(actions):
        record = await execute_approved(
            env.approval(action, id=uuid.UUID(int=10 + i)), action, **env.kwargs()
        )
        out.append((str(record.id), record.status, record.external_ref, record.error_kind))
    out.append([(r.seq, r.tool, r.outcome, r.error_type) for r in env.conns.calls.records])
    out.append(env.diff())
    return tuple(out)


async def test_deterministic_for_same_seed_and_order(make_env: MakeEnv) -> None:
    assert await scenario(make_env) == await scenario(make_env)


async def test_error_records_contain_no_content(make_env: MakeEnv) -> None:
    env = make_env(faults({"tool": "send_draft", "error": "transient"}))
    record = await run(env, MAIL)
    text = repr(record)
    assert "bob@example.org" not in text and "합성 본문" not in text and "합성 제목" not in text
    assert ACCOUNT not in text


async def test_modify_already_at_target_does_not_write(make_env: MakeEnv) -> None:
    env = make_env()
    current = await env.calendar.get_event(CALENDAR, EVENT_ID)
    same = UpdateEventTime(
        calendar_id=CALENDAR, event_id=EVENT_ID, start=current.start, end=current.end
    )
    record = await run(env, same)
    assert record.status == SUCCEEDED
    assert env.calls("update_event") == []
    assert "before_version" not in record.external_ref


async def test_cancel_already_cancelled_does_not_write(make_env: MakeEnv) -> None:
    env = make_env()
    event = await env.calendar.get_event(CALENDAR, EVENT_ID)
    await env.calendar.cancel_event(CALENDAR, EVENT_ID, if_match=event.version)
    record = await run(env, CANCEL)
    assert record.status == SUCCEEDED
    assert env.calls("cancel_event") == ["cancel_event"]  # 선행 호출뿐


async def _draft_for_resend(env: Env, **overrides: Any) -> ExecutionRecord:
    pending = await seed_pending(env, MAIL)
    draft = await env.mail.create_draft(mail_spec(pending.id.hex, **overrides))
    await env.store.record_ref(
        pending.id, DEAD, {"draft_id": draft.draft_id, "thread_id": draft.thread_id}
    )
    return pending


@pytest.mark.parametrize(
    "overrides",
    [
        {"to": (Address(email="evil@example.net"),)},
        {"to": (Address(email="bob@example.org"), Address(email="evil@example.net"))},
        {"cc": (Address(email="evil@example.net"),)},
        {"subject": "다른 제목"},
        {"body_text": "바뀐 본문"},
    ],
)
async def test_resend_with_changed_draft_needs_user(
    make_env: MakeEnv, overrides: dict[str, Any]
) -> None:
    env = make_env()
    await _draft_for_resend(env, **overrides)
    record = await run(env, MAIL)
    assert record.status == NEEDS_USER
    assert env.calls("send_draft") == []


async def test_resend_with_unreadable_draft_content_needs_user(make_env: MakeEnv) -> None:
    env = make_env()
    await _draft_for_resend(env)
    mail = env.mail

    class NoSpec:
        def __getattr__(self, name: str) -> Any:
            return getattr(mail, name)

        async def get_draft(self, draft_id: str) -> Any:
            return (await mail.get_draft(draft_id)).model_copy(update={"spec": None})

    record = await execute_approved(
        env.approval(MAIL),
        MAIL,
        calendar=env.calendar,
        mail=NoSpec(),
        store=env.store,
        clock=env.clock,
    )
    assert record.status == NEEDS_USER
    assert env.calls("send_draft") == []


async def test_resend_with_same_draft_in_other_case(make_env: MakeEnv) -> None:
    env = make_env()
    await _draft_for_resend(env, to=(Address(email="BOB@example.org"),))
    record = await run(env, MAIL)
    assert record.status == SUCCEEDED
