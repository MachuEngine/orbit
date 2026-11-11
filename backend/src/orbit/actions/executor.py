"""승인된 쓰기 실행기 (ADR-0012 3절, ADR-0003 조건 1, SPIKE-005).

순서: 승인·만료·해시 검사 -> `execution` 선기록 -> 외부 호출 -> 결과 기록.
결과가 불명확하거나(`ResultUnknown`) `pending`·`unknown` 행으로 다시 들어오면 재호출하지 않고
대조한다. 자동 재호출은 중복이 구조적으로 불가능한 표의 "1회 재호출"뿐이다.
판단할 수 없으면 `needs_user`다.

판단 기록:
- 만료는 `execution` 행이 아직 없을 때만 검사한다. 행이 이미 있으면(크래시 뒤 재진입) 만료돼도
  대조를 끝내야 하므로 만료로 거절하지 않는다. 승인 상태·해시 검사는 항상 한다.
- 대조 중 읽기 호출이 `ResultUnknown` 말고 다른 오류로 실패하면(일시 오류 등) 결론을 내리지 않고
  `unknown`으로 기록한다. 다음 재진입에서 다시 대조한다.
- 일정 생성 대조는 요청한 참석자가 현재 일정에 모두 있으면 같은 내용으로 본다(주최자 자동 추가 등
  제공자가 붙이는 참석자를 허용하기 위함).
- 같은 프로세스에서 같은 승인을 동시에 실행하면 승인 ID별 잠금으로 직렬화한다. 한쪽이 끝난 뒤 다른
  쪽은 종료된 행을 그대로 돌려받는다. 프로세스 사이 중복은 4C 워커와 DB(`begin`의 유일 제약) 몫이다.
- 오류 종류는 예외 클래스 이름만 `error_kind`에 남긴다. 메시지·본문·주소는 기록하지 않는다.
"""

import asyncio
import dataclasses
import unicodedata
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orbit.actions.hashing import args_hash, normalize_email
from orbit.actions.models import CancelEvent, CreateEvent, SendMail, UpdateEventTime, WriteAction
from orbit.actions.store import (
    FAILED,
    NEEDS_USER,
    SUCCEEDED,
    TERMINAL,
    UNKNOWN,
    ExecutionRecord,
    ExecutionStore,
)
from orbit.connectors.contracts import (
    ORBIT_EXECUTION_HEADER,
    Address,
    AlreadyExists,
    Attendee,
    CalendarConnector,
    CalendarEvent,
    ConnectorError,
    DraftSpec,
    EventDraft,
    EventPatch,
    MailConnector,
    NotFound,
    PreconditionFailed,
    ResultUnknown,
    event_id_from_uuid,
)
from orbit.core.clock import Clock


@dataclass(frozen=True)
class ApprovalView:
    """실행기가 보는 승인 정보. 승인 행 전체가 아니라 판단에 필요한 값만 가진다."""

    id: uuid.UUID
    status: str
    args_hash: str
    expires_at: datetime


class ApprovalRejected(Exception):  # noqa: N818
    """승인 검사 실패. 외부 호출과 `execution` 행 생성 없이 거절한다.

    `reason`: `not_approved`, `expired`, `hash_mismatch`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- 공통


class _Run:
    """한 번의 실행 상태. `ref`는 `external_ref`의 로컬 사본이고 기록할 때 통째로 저장한다."""

    def __init__(self, store: ExecutionStore, record: ExecutionRecord) -> None:
        self.store = store
        self.record = record
        self.ref: dict[str, Any] = dict(record.external_ref)

    @property
    def hex_id(self) -> str:
        return event_id_from_uuid(self.record.id)

    async def save_ref(self, **updates: Any) -> None:
        self.ref.update(updates)
        await self.store.record_ref(self.record.id, dict(self.ref))

    async def end(
        self, status: str, error: Exception | None = None, **ref_updates: Any
    ) -> ExecutionRecord:
        self.ref.update(ref_updates)
        kind = None if error is None else type(error).__name__
        await self.store.finish(self.record.id, status, dict(self.ref), kind)
        self.record = dataclasses.replace(
            self.record, status=status, external_ref=dict(self.ref), error_kind=kind
        )
        return self.record


_locks: weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock] = weakref.WeakValueDictionary()


async def execute_approved(
    approval: ApprovalView,
    action: WriteAction,
    *,
    calendar: CalendarConnector,
    mail: MailConnector,
    store: ExecutionStore,
    clock: Clock,
) -> ExecutionRecord:
    if approval.status == "expired":
        raise ApprovalRejected("expired")
    if approval.status != "approved":
        raise ApprovalRejected("not_approved")
    lock = _locks.get(approval.id)
    if lock is None:
        lock = _locks[approval.id] = asyncio.Lock()
    async with lock:
        return await _execute_locked(approval, action, calendar, mail, store, clock)


async def _execute_locked(
    approval: ApprovalView,
    action: WriteAction,
    calendar: CalendarConnector,
    mail: MailConnector,
    store: ExecutionStore,
    clock: Clock,
) -> ExecutionRecord:
    existing = await store.get(approval.id)
    if existing is None and clock.now() >= approval.expires_at:
        raise ApprovalRejected("expired")
    if args_hash(action) != approval.args_hash:
        raise ApprovalRejected("hash_mismatch")

    record, created = await store.begin(approval.id)
    if not created and record.status in TERMINAL:
        return record
    run = _Run(store, record)
    # 새 행이면 처음부터, 기존 pending·unknown 행이면 대조부터 시작한다.
    resume = not created
    match action:
        case CreateEvent():
            return await _create_event(run, calendar, action, resume)
        case UpdateEventTime() | CancelEvent():
            return await _modify_event(run, calendar, action)
        case SendMail():
            return await _send_mail(run, mail, action, resume)


# ---------------------------------------------------------------- 일정 생성


def _event_draft(action: CreateEvent) -> EventDraft:
    return EventDraft(
        title=action.title,
        start=action.start,
        end=action.end,
        description=action.description or "",
        location=action.location or "",
        attendees=tuple(Attendee(email=e) for e in action.attendees),
    )


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _same_created(event: CalendarEvent, action: CreateEvent) -> bool:
    have = {normalize_email(a.email) for a in event.attendees}
    want = {normalize_email(e) for e in action.attendees}
    return (
        event.status == "confirmed"
        and _nfc(event.title) == _nfc(action.title)
        and event.start == action.start
        and event.end == action.end
        and _nfc(event.description) == _nfc(action.description or "")
        and _nfc(event.location) == _nfc(action.location or "")
        and want <= have
    )


async def _create_event(
    run: _Run, calendar: CalendarConnector, action: CreateEvent, resume: bool
) -> ExecutionRecord:
    event_id = run.hex_id
    if not resume:
        try:
            event = await calendar.create_event(action.calendar_id, event_id, _event_draft(action))
        except (ResultUnknown, AlreadyExists):
            pass  # 아래 대조로 간다
        except (ConnectorError, ValueError) as e:
            return await run.end(FAILED, e)
        else:
            return await _ok_event(run, event)
    return await _reconcile_create(run, calendar, action)


async def _ok_event(run: _Run, event: CalendarEvent) -> ExecutionRecord:
    return await run.end(
        SUCCEEDED,
        None,
        calendar_id=event.calendar_id,
        event_id=event.id,
        version=event.version,
    )


async def _compare_created(
    run: _Run, calendar: CalendarConnector, action: CreateEvent
) -> ExecutionRecord:
    try:
        event = await calendar.get_event(action.calendar_id, run.hex_id)
    except ConnectorError as e:
        return await run.end(UNKNOWN, e)
    if _same_created(event, action):
        return await _ok_event(run, event)
    return await run.end(NEEDS_USER)


async def _reconcile_create(
    run: _Run, calendar: CalendarConnector, action: CreateEvent
) -> ExecutionRecord:
    try:
        event = await calendar.get_event(action.calendar_id, run.hex_id)
    except NotFound:
        pass
    except ConnectorError as e:
        return await run.end(UNKNOWN, e)
    else:
        if _same_created(event, action):
            return await _ok_event(run, event)
        return await run.end(NEEDS_USER)
    # 없다: 같은 ID로 다시 만들어도 중복이 생기지 않으므로 1회 재호출한다.
    try:
        event = await calendar.create_event(action.calendar_id, run.hex_id, _event_draft(action))
    except AlreadyExists:
        return await _compare_created(run, calendar, action)
    except ResultUnknown as e:
        return await run.end(NEEDS_USER, e)
    except (ConnectorError, ValueError) as e:
        return await run.end(FAILED, e)
    return await _ok_event(run, event)


# ---------------------------------------------------------------- 일정 시각 변경·취소


def _matches_target(event: CalendarEvent, action: UpdateEventTime | CancelEvent) -> bool:
    if isinstance(action, CancelEvent):
        return event.status == "cancelled"
    return event.status == "confirmed" and event.start == action.start and event.end == action.end


async def _call_modify(
    calendar: CalendarConnector, action: UpdateEventTime | CancelEvent, if_match: str
) -> CalendarEvent:
    if isinstance(action, CancelEvent):
        return await calendar.cancel_event(action.calendar_id, action.event_id, if_match=if_match)
    patch = EventPatch(start=action.start, end=action.end)
    return await calendar.update_event(
        action.calendar_id, action.event_id, patch, if_match=if_match
    )


async def _modify_event(
    run: _Run, calendar: CalendarConnector, action: UpdateEventTime | CancelEvent
) -> ExecutionRecord:
    # before_version이 기록되기 전이면 외부 쓰기를 한 적이 없다(기록이 호출보다 먼저다).
    if "before_version" not in run.ref:
        try:
            current = await calendar.get_event(action.calendar_id, action.event_id)
        except ConnectorError as e:
            return await run.end(FAILED, e)
        await run.save_ref(
            calendar_id=action.calendar_id,
            event_id=action.event_id,
            before_version=current.version,
        )
        try:
            event = await _call_modify(calendar, action, current.version)
        except ResultUnknown:
            return await _reconcile_modify(run, calendar, action)
        except (ConnectorError, ValueError) as e:  # PreconditionFailed 포함: 재호출하지 않는다
            return await run.end(FAILED, e)
        return await run.end(SUCCEEDED, None, version=event.version)
    return await _reconcile_modify(run, calendar, action)


async def _reconcile_modify(
    run: _Run, calendar: CalendarConnector, action: UpdateEventTime | CancelEvent
) -> ExecutionRecord:
    before = str(run.ref["before_version"])
    try:
        current = await calendar.get_event(action.calendar_id, action.event_id)
    except NotFound as e:
        return await run.end(NEEDS_USER, e)
    except ConnectorError as e:
        return await run.end(UNKNOWN, e)
    if _matches_target(current, action):
        return await run.end(SUCCEEDED, None, version=current.version)
    if current.version != before:
        return await run.end(NEEDS_USER)  # 모르는 다른 변경이 있다
    # 적용되지 않았다: 같은 if_match로 1회 재호출한다(중복 적용 불가).
    try:
        event = await _call_modify(calendar, action, before)
    except ResultUnknown as e:
        return await run.end(NEEDS_USER, e)
    except PreconditionFailed as e:
        try:
            current = await calendar.get_event(action.calendar_id, action.event_id)
        except ConnectorError as read_error:
            return await run.end(UNKNOWN, read_error)
        if _matches_target(current, action):
            return await run.end(SUCCEEDED, None, version=current.version)
        return await run.end(NEEDS_USER, e)
    except (ConnectorError, ValueError) as e:
        return await run.end(FAILED, e)
    return await run.end(SUCCEEDED, None, version=event.version)


# ---------------------------------------------------------------- 메일 발송


async def _send_mail(
    run: _Run, mail: MailConnector, action: SendMail, resume: bool
) -> ExecutionRecord:
    if "draft_id" not in run.ref:
        # 초안 ID를 기록하기 전이다. 초안이 만들어졌는지 알 수 없지만 발송 전이므로 새로 만든다.
        return await _create_draft_and_send(run, mail, action, resume)
    return await _reconcile_send(run, mail)


async def _draft_spec(run: _Run, mail: MailConnector, action: SendMail) -> DraftSpec:
    reply_to: str | None = None
    if action.reply_to_thread_id is not None:
        thread = await mail.get_thread(action.reply_to_thread_id)
        if not thread.messages:
            raise NotFound("답장 대상 스레드에 메시지가 없다")
        reply_to = thread.messages[-1].id
    return DraftSpec(
        to=tuple(Address(email=e) for e in action.to),
        cc=tuple(Address(email=e) for e in action.cc),
        subject=action.subject,
        body_text=action.body,
        reply_to_message_id=reply_to,
        execution_id=run.hex_id,
    )


async def _create_draft_and_send(
    run: _Run, mail: MailConnector, action: SendMail, resume: bool
) -> ExecutionRecord:
    try:
        spec = await _draft_spec(run, mail, action)
    except (ConnectorError, ValueError) as e:
        return await run.end(FAILED, e)  # 쓰기 전이다

    attempts = 1 if resume else 2  # 재진입이면 이미 한 번 시도했을 수 있다
    if resume:
        await run.save_ref(orphan_draft_possible=True)
    while True:
        try:
            draft = await mail.create_draft(spec)
            break
        except ResultUnknown as e:
            attempts -= 1
            if attempts <= 0:
                return await run.end(NEEDS_USER, e)
            await run.save_ref(orphan_draft_possible=True)
        except (ConnectorError, ValueError) as e:
            return await run.end(FAILED, e)

    await run.save_ref(draft_id=draft.draft_id, thread_id=draft.thread_id)
    try:
        message = await mail.send_draft(draft.draft_id)
    except ResultUnknown:
        return await _reconcile_send(run, mail)
    except (ConnectorError, ValueError) as e:
        return await run.end(FAILED, e)
    return await run.end(SUCCEEDED, None, sent_message_id=message.id)


async def _find_sent(run: _Run, mail: MailConnector) -> ExecutionRecord:
    try:
        thread = await mail.get_thread(str(run.ref["thread_id"]))
    except NotFound as e:
        return await run.end(NEEDS_USER, e)
    except ConnectorError as e:
        return await run.end(UNKNOWN, e)
    wanted = ORBIT_EXECUTION_HEADER.lower()
    for message in thread.messages:
        headers = {k.lower(): v for k, v in message.headers.items()}
        if "SENT" in message.labels and headers.get(wanted) == run.hex_id:
            return await run.end(SUCCEEDED, None, sent_message_id=message.id)
    return await run.end(NEEDS_USER)  # 초안이 사라졌고 보낸 메일도 없다: 사용자가 지웠을 수 있다


async def _reconcile_send(run: _Run, mail: MailConnector) -> ExecutionRecord:
    draft_id = str(run.ref["draft_id"])
    try:
        await mail.get_draft(draft_id)
    except NotFound:
        return await _find_sent(run, mail)
    except ConnectorError as e:
        return await run.end(UNKNOWN, e)
    # 초안이 남아 있다: 미발송이다. 발송된 초안은 재발송이 거절되므로 1회 재호출은 중복이 아니다.
    try:
        message = await mail.send_draft(draft_id)
    except NotFound:
        return await _find_sent(run, mail)
    except ResultUnknown as e:
        return await run.end(NEEDS_USER, e)
    except (ConnectorError, ValueError) as e:
        return await run.end(FAILED, e)
    return await run.end(SUCCEEDED, None, sent_message_id=message.id)
