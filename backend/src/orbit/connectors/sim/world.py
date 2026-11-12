"""SimWorld: JSON 스냅샷에서 읽는 인메모리 메일함·캘린더 (ADR-0008 5절).

스냅샷 스키마 v1:

    {
      "schema_version": 1,
      "seed": 7,                                  # 정수. 로드한 월드의 ID 생성기 시드
      "accounts": {
        "<account_id>": {
          "email": "user@example.com",
          "calendars": [Calendar, ...],           # contracts.Calendar
          "events":    [CalendarEvent, ...],      # calendar_id는 calendars에 있어야 함
          "messages":  [MailMessage, ...],        # contracts.MailMessage (JSON 키는 "from")
          "threads":   [{"thread_id": "...", "message_ids": ["..."]}, ...],
          "drafts":    [Draft, ...],              # contracts.Draft, "spec"(DraftSpec) 필수
          "files":     [SimFile, ...]             # 선택. 문서(Drive) 파일(ADR-0020), 없으면 빈 목록
        }
      }
    }

- 시각은 시간대가 있는 ISO 8601 문자열이어야 한다.
- 메시지의 `thread_id`는 `threads`에 있어야 하고 그 스레드의 `message_ids`에 포함돼야 한다.
- 로드 검증이 실패하면 `SnapshotError`를 던진다.

결정성: 시각은 주입한 `Clock`에서만 얻고, Sim이 만드는 ID(메시지·초안·스레드·버전)는
에피소드 시드 기반 `IdGenerator`가 만든다. 같은 스냅샷·시드·호출 순서면 같은 ID가 나온다.
"""

from __future__ import annotations

import copy
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from orbit.connectors.contracts import (
    Address,
    Calendar,
    CalendarEvent,
    Draft,
    MailMessage,
)
from orbit.core.clock import Clock

SCHEMA_VERSION = 1


class SnapshotError(ValueError):
    """스냅샷 스키마·참조 검증 실패."""


class IdGenerator:
    """에피소드 시드 기반 결정적 ID 생성기. 호출 순서가 같으면 값이 같다."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)  # noqa: S311  재현성용 의사난수, 보안 용도 아님

    def next_id(self, prefix: str) -> str:
        return f"{prefix}{self._rng.getrandbits(64):016x}"

    def message_id(self) -> str:
        return self.next_id("msg")

    def draft_id(self) -> str:
        return self.next_id("drf")

    def thread_id(self) -> str:
        return self.next_id("thr")

    def version(self) -> str:
        return self.next_id("v")


# ---------------------------------------------------------------- 스냅샷 스키마


class _ThreadSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    message_ids: list[str]


class DraftLogEntry(BaseModel):
    """초안 생애 기록 한 건. `diff`가 생성·발송·삭제를 추론 없이 정확히 짝짓는 데 쓴다.

    `draft`는 그 시점의 초안 내용이다(생성 후 발송·삭제되면 `drafts`에서 사라지므로 따로 담는다).
    `sent_message_id`는 `kind == "sent"`일 때 발송으로 생긴 메시지 ID다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    kind: Literal["created", "sent", "deleted"]
    draft_id: str
    thread_id: str
    draft: Draft
    sent_message_id: str | None = None


class SimFile(BaseModel):
    """Sim 문서 파일(ADR-0020). `text`는 내보내기·읽기 결과다. Sheets는 첫 시트 CSV다.

    합성 데이터만 쓴다. `size_bytes`가 없으면 `text`의 UTF-8 길이다.
    한도 초과 같은 시나리오는 이 값을 직접 지정한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    mime_type: str
    modified_at: AwareDatetime
    owners: tuple[Address, ...] = ()
    text: str = ""
    size_bytes: int | None = Field(default=None, ge=0)
    trashed: bool = False


class _AccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    calendars: list[Calendar] = []
    events: list[CalendarEvent] = []
    messages: list[MailMessage] = []
    threads: list[_ThreadSnapshot] = []
    drafts: list[Draft] = []
    draft_log: list[DraftLogEntry] = []  # 스냅샷 v1에 없으면 빈 기록
    files: list[SimFile] = []  # 스냅샷 v1에 없으면 빈 목록(기존 평가 스냅샷은 바뀌지 않는다)


class _WorldSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    seed: int
    accounts: dict[str, _AccountSnapshot]


HistoryKind = Literal["added", "deleted", "labels_added", "labels_removed"]


@dataclass(frozen=True)
class HistoryEntry:
    """메일 변경 이력 한 건(ADR-0019 §2). `seq`는 이 항목을 기록한 뒤의 이력 위치다."""

    seq: int
    kind: HistoryKind
    message_id: str


@dataclass
class AccountState:
    """계정 하나의 변경 가능한 상태. Sim 커넥터가 직접 읽고 쓴다.

    변경 이력(`history`)은 색인 동기화 재현용이다. 월드 스냅샷에는 담지 않으므로 불러온 월드의
    이력 위치는 0이다. 이력 위치(`history_id`)는 변경이나 만료 주입이 있을 때마다 1씩 커진다.
    이력 만료는 `expire_history`로 주입한다.
    """

    email: str
    calendars: dict[str, Calendar] = field(default_factory=dict)
    events: dict[str, dict[str, CalendarEvent]] = field(default_factory=dict)  # calendar -> event
    messages: dict[str, MailMessage] = field(default_factory=dict)
    threads: dict[str, list[str]] = field(default_factory=dict)  # thread_id -> message_ids
    drafts: dict[str, Draft] = field(default_factory=dict)
    draft_log: list[DraftLogEntry] = field(default_factory=list)
    files: dict[str, SimFile] = field(default_factory=dict)
    history: list[HistoryEntry] = field(default_factory=list)
    history_id: int = 0  # 지금 이력 위치
    history_floor: int = 0  # 이 위치보다 앞선 이력 위치로는 이어 읽을 수 없다(만료)
    user_labels: dict[str, str] = field(default_factory=dict)  # 사용자 라벨 ID -> 이름

    # ---- 변경 이력 (색인 동기화 시나리오용). 월드를 바꾸는 쪽이 이 메서드로 바꾼다.

    def record_history(self, kind: HistoryKind, message_id: str) -> None:
        self.history_id += 1
        self.history.append(HistoryEntry(self.history_id, kind, message_id))

    def expire_history(self) -> None:
        """지금까지 발급한 이력 위치를 모두 만료시킨다.

        이전 위치로 `list_history`를 부르면 `HistoryExpired`다. 만료 뒤에 `current_history_id`로
        받은 위치부터는 다시 읽을 수 있다.
        """
        self.history_id += 1
        self.history_floor = self.history_id

    def add_message(self, message: MailMessage) -> None:
        """메시지를 메일함에 넣고 `added`를 기록한다."""
        if message.id in self.messages:
            raise ValueError(f"메시지 ID 중복 {message.id}")
        self.messages[message.id] = message
        self.threads.setdefault(message.thread_id, []).append(message.id)
        self.record_history("added", message.id)

    def delete_message(self, message_id: str) -> None:
        message = self.messages.pop(message_id)
        ids = self.threads.get(message.thread_id, [])
        if message_id in ids:
            ids.remove(message_id)
        self.record_history("deleted", message_id)

    def set_labels(
        self, message_id: str, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()
    ) -> None:
        """라벨을 붙이거나 뗀다. 실제로 바뀐 것만 이력에 남긴다."""
        message = self.messages[message_id]
        labels = [label for label in message.labels if label not in remove]
        added = [label for label in add if label not in labels]
        removed = [label for label in remove if label in message.labels]
        labels += added
        self.messages[message_id] = message.model_copy(update={"labels": tuple(labels)})
        if added:
            self.record_history("labels_added", message_id)
        if removed:
            self.record_history("labels_removed", message_id)

    def record_draft(
        self,
        kind: Literal["created", "sent", "deleted"],
        draft: Draft,
        *,
        sent_message_id: str | None = None,
    ) -> None:
        """초안 생애 기록을 남긴다. Sim 커넥터가 초안 상태를 바꿀 때 함께 호출한다."""
        self.draft_log.append(
            DraftLogEntry(
                seq=len(self.draft_log) + 1,
                kind=kind,
                draft_id=draft.draft_id,
                thread_id=draft.thread_id,
                draft=draft,
                sent_message_id=sent_message_id,
            )
        )

    @classmethod
    def from_snapshot(cls, snap: _AccountSnapshot, label: str) -> AccountState:
        state = cls(email=snap.email)
        for cal in snap.calendars:
            if cal.id in state.calendars:
                raise SnapshotError(f"{label}: 캘린더 ID 중복 {cal.id}")
            state.calendars[cal.id] = cal
            state.events[cal.id] = {}
        for ev in snap.events:
            if ev.calendar_id not in state.calendars:
                raise SnapshotError(f"{label}: 일정 {ev.id}의 캘린더 {ev.calendar_id}가 없다")
            if ev.id in state.events[ev.calendar_id]:
                raise SnapshotError(f"{label}: 일정 ID 중복 {ev.id}")
            state.events[ev.calendar_id][ev.id] = ev
        for th in snap.threads:
            if th.thread_id in state.threads:
                raise SnapshotError(f"{label}: 스레드 ID 중복 {th.thread_id}")
            state.threads[th.thread_id] = list(th.message_ids)
        for msg in snap.messages:
            if msg.id in state.messages:
                raise SnapshotError(f"{label}: 메시지 ID 중복 {msg.id}")
            state.messages[msg.id] = msg
        for thread_id, ids in state.threads.items():
            for mid in ids:
                if mid not in state.messages:
                    raise SnapshotError(f"{label}: 스레드 {thread_id}의 메시지 {mid}가 없다")
        for msg in state.messages.values():
            if msg.id not in state.threads.get(msg.thread_id, []):
                raise SnapshotError(f"{label}: 메시지 {msg.id}가 스레드 {msg.thread_id}에 없다")
        for draft in snap.drafts:
            if draft.draft_id in state.drafts:
                raise SnapshotError(f"{label}: 초안 ID 중복 {draft.draft_id}")
            if draft.spec is None:
                raise SnapshotError(f"{label}: 초안 {draft.draft_id}에 spec이 없다")
            state.drafts[draft.draft_id] = draft
        seqs = [e.seq for e in snap.draft_log]
        if seqs != list(range(1, len(seqs) + 1)):
            raise SnapshotError(f"{label}: draft_log의 seq는 1부터 연속이어야 한다")
        for entry in snap.draft_log:
            if entry.kind == "sent" and entry.sent_message_id not in state.messages:
                raise SnapshotError(
                    f"{label}: draft_log {entry.seq}의 sent_message_id가 messages에 없다"
                )
        state.draft_log = list(snap.draft_log)
        for file in snap.files:
            if file.id in state.files:
                raise SnapshotError(f"{label}: 파일 ID 중복 {file.id}")
            state.files[file.id] = file
        return state

    def to_snapshot(self) -> dict[str, Any]:
        def dump(m: BaseModel) -> dict[str, Any]:
            return m.model_dump(mode="json", by_alias=True)

        snapshot: dict[str, Any] = {
            "email": self.email,
            "calendars": [dump(c) for c in self.calendars.values()],
            "events": [dump(e) for evs in self.events.values() for e in evs.values()],
            "messages": [dump(m) for m in self.messages.values()],
            "threads": [
                {"thread_id": t, "message_ids": list(ids)} for t, ids in self.threads.items()
            ],
            "drafts": [dump(d) for d in self.drafts.values()],
        }
        if self.draft_log:  # 기록이 없으면 v1 스냅샷과 같은 모양을 유지한다
            snapshot["draft_log"] = [dump(e) for e in self.draft_log]
        if self.files:  # 파일이 없으면 기존 스냅샷과 같은 모양(해시 불변)을 유지한다
            snapshot["files"] = [dump(f) for f in self.files.values()]
        return snapshot


class SimWorld:
    def __init__(self, seed: int, accounts: dict[str, AccountState], clock: Clock) -> None:
        self.seed = seed
        self.clock = clock
        self.ids = IdGenerator(seed)
        self._accounts = accounts

    @classmethod
    def load(cls, source: str | Path | Mapping[str, Any], clock: Clock) -> SimWorld:
        """경로(JSON 파일) 또는 dict에서 월드를 만든다. 검증 실패는 `SnapshotError`."""
        if isinstance(source, str | Path):
            try:
                data: Any = json.loads(Path(source).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SnapshotError(f"스냅샷을 읽을 수 없다: {exc}") from exc
        else:
            data = dict(source)
        if not isinstance(data, dict):
            raise SnapshotError("스냅샷 최상위는 객체여야 한다")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise SnapshotError(
                f"지원하지 않는 schema_version: {data.get('schema_version')!r} "
                f"(필요: {SCHEMA_VERSION})"
            )
        try:
            snap = _WorldSnapshot.model_validate(data, strict=False)
        except ValidationError as exc:
            raise SnapshotError(f"스냅샷 검증 실패: {exc}") from exc
        accounts = {
            aid: AccountState.from_snapshot(acc, f"계정 {aid}")
            for aid, acc in snap.accounts.items()
        }
        return cls(snap.seed, accounts, clock)

    @property
    def account_ids(self) -> list[str]:
        return list(self._accounts)

    def account(self, account_id: str) -> AccountState:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise KeyError(f"없는 계정: {account_id}") from None

    def fork(self, episode_seed: int, clock: Clock | None = None) -> SimWorld:
        """독립 복사본(깊은 복사)을 만든다. ID 생성기는 `episode_seed`로 새로 시작한다.

        `clock`을 주지 않으면 시계를 복사하지 않고 같은 `Clock` 객체를 공유한다. 병렬 에피소드가
        한 시계를 함께 움직이면 재현성이 깨지므로(D1-5), 병렬 에피소드는 각자 시계를 만들어
        `clock`으로 넘긴다.
        """
        return SimWorld(
            episode_seed, copy.deepcopy(self._accounts), clock if clock is not None else self.clock
        )

    def copy(self) -> SimWorld:
        """ID 생성기 상태까지 그대로 복사한다. `diff`의 `before` 기준점으로 쓴다.

        시계는 복사하지 않고 공유한다. `diff` 기준점은 읽기만 하므로 문제없지만, 복사본을 별도
        에피소드로 쓴다면 시계를 공유하지 않도록 `fork(seed, clock=...)`를 쓴다(D1-5).
        """
        return copy.deepcopy(self, {id(self.clock): self.clock})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "seed": self.seed,
            "accounts": {aid: acc.to_snapshot() for aid, acc in self._accounts.items()},
        }


# ---------------------------------------------------------------- 상태 비교


@dataclass(frozen=True)
class Normalization:
    """비교에서 제외할 비결정 필드 규칙 (D1-3)."""

    ignore_generated_ids: bool = True  # Sim이 만든 메시지·초안·스레드 ID
    ignore_volatile_fields: bool = True  # 일정의 updated_at, version


@dataclass(frozen=True)
class FieldChange:
    before: Any
    after: Any


@dataclass(frozen=True)
class EventCreated:
    account_id: str
    event: dict[str, Any]


@dataclass(frozen=True)
class EventUpdated:
    account_id: str
    calendar_id: str
    event_id: str
    changes: dict[str, FieldChange]


@dataclass(frozen=True)
class EventCancelled:
    account_id: str
    calendar_id: str
    event_id: str


@dataclass(frozen=True)
class DraftCreated:
    account_id: str
    draft: dict[str, Any]


@dataclass(frozen=True)
class DraftDeleted:
    account_id: str
    draft: dict[str, Any]


@dataclass(frozen=True)
class DraftSent:
    account_id: str
    draft: dict[str, Any]
    message: dict[str, Any]  # 발송으로 생긴 메시지


@dataclass(frozen=True)
class MessageAdded:
    account_id: str
    message: dict[str, Any]


@dataclass(frozen=True)
class WorldDiff:
    """구조화된 변경 목록. 발송으로 생긴 메시지는 `drafts_sent`에만 들어간다."""

    events_created: tuple[EventCreated, ...] = ()
    events_updated: tuple[EventUpdated, ...] = ()
    events_cancelled: tuple[EventCancelled, ...] = ()
    drafts_created: tuple[DraftCreated, ...] = ()
    drafts_deleted: tuple[DraftDeleted, ...] = ()
    drafts_sent: tuple[DraftSent, ...] = ()
    messages_added: tuple[MessageAdded, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in self.__dataclass_fields__)


_VOLATILE_EVENT_FIELDS = ("updated_at", "version")


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


def _key(item: dict[str, Any]) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def diff(before: SimWorld, after: SimWorld, *, normalize: Normalization | None = None) -> WorldDiff:
    """두 월드의 차이를 구조화한다. 정렬은 결정적이다(계정 ID, 대상 ID, 정규화된 내용 순)."""
    norm = normalize if normalize is not None else Normalization()
    events_created: list[EventCreated] = []
    events_updated: list[EventUpdated] = []
    events_cancelled: list[EventCancelled] = []
    drafts_created: list[DraftCreated] = []
    drafts_deleted: list[DraftDeleted] = []
    drafts_sent: list[DraftSent] = []
    messages_added: list[MessageAdded] = []

    for aid in sorted(set(before.account_ids) | set(after.account_ids)):
        b = before.account(aid) if aid in before.account_ids else AccountState(email="")
        a = after.account(aid) if aid in after.account_ids else AccountState(email="")
        _diff_events(aid, b, a, norm, events_created, events_updated, events_cancelled)
        _diff_mail(aid, b, a, norm, drafts_created, drafts_deleted, drafts_sent, messages_added)

    return WorldDiff(
        events_created=tuple(sorted(events_created, key=lambda x: (x.account_id, _key(x.event)))),
        events_updated=tuple(
            sorted(events_updated, key=lambda x: (x.account_id, x.calendar_id, x.event_id))
        ),
        events_cancelled=tuple(
            sorted(events_cancelled, key=lambda x: (x.account_id, x.calendar_id, x.event_id))
        ),
        drafts_created=tuple(sorted(drafts_created, key=lambda x: (x.account_id, _key(x.draft)))),
        drafts_deleted=tuple(sorted(drafts_deleted, key=lambda x: (x.account_id, _key(x.draft)))),
        drafts_sent=tuple(sorted(drafts_sent, key=lambda x: (x.account_id, _key(x.draft)))),
        messages_added=tuple(sorted(messages_added, key=lambda x: (x.account_id, _key(x.message)))),
    )


def _diff_events(
    aid: str,
    b: AccountState,
    a: AccountState,
    norm: Normalization,
    created: list[EventCreated],
    updated: list[EventUpdated],
    cancelled: list[EventCancelled],
) -> None:
    for cal_id, events in a.events.items():
        old_events = b.events.get(cal_id, {})
        for ev_id, ev in events.items():
            new = _dump(ev)
            old_ev = old_events.get(ev_id)
            if old_ev is None:
                # 일정 ID는 클라이언트가 정하므로 정규화로 지우지 않는다.
                if norm.ignore_volatile_fields:
                    for f in _VOLATILE_EVENT_FIELDS:
                        new.pop(f)
                created.append(EventCreated(aid, new))
                continue
            old = _dump(old_ev)
            changes = {
                f: FieldChange(old[f], new[f])
                for f in new
                if f not in ("id", "calendar_id") and old[f] != new[f]
            }
            if norm.ignore_volatile_fields:
                for f in _VOLATILE_EVENT_FIELDS:
                    changes.pop(f, None)
            if "status" in changes and changes["status"].after == "cancelled":
                cancelled.append(EventCancelled(aid, cal_id, ev_id))
                del changes["status"]
            if changes:
                updated.append(EventUpdated(aid, cal_id, ev_id, changes))


def _same_content(draft: Draft, msg: MailMessage) -> bool:
    return (
        draft.spec is not None
        and draft.spec.subject == msg.subject
        and draft.spec.body_text == msg.body_text
    )


def _diff_mail(
    aid: str,
    b: AccountState,
    a: AccountState,
    norm: Normalization,
    drafts_created: list[DraftCreated],
    drafts_deleted: list[DraftDeleted],
    drafts_sent: list[DraftSent],
    messages_added: list[MessageAdded],
) -> None:
    new_messages = {mid: m for mid, m in a.messages.items() if mid not in b.messages}
    claimed: set[str] = set()

    def clean_message(msg: MailMessage) -> dict[str, Any]:
        d = _dump(msg)
        if norm.ignore_generated_ids:
            d.pop("id")
            if msg.thread_id not in b.threads:
                d.pop("thread_id")
        return d

    def clean_draft(draft: Draft) -> dict[str, Any]:
        d = _dump(draft)
        if norm.ignore_generated_ids:
            d.pop("draft_id")
            d.pop("message_id")
            if draft.thread_id not in b.threads:
                d.pop("thread_id")
        return d

    # 1) 초안 생애 기록: after에만 있는 항목은 추론 없이 정확히 짝지어진다.
    before_seqs = {e.seq for e in b.draft_log}
    logged_created: set[str] = set()
    logged_ended: set[str] = set()
    for entry in a.draft_log:
        if entry.seq in before_seqs:
            continue
        if entry.kind == "created":
            logged_created.add(entry.draft_id)
            drafts_created.append(DraftCreated(aid, clean_draft(entry.draft)))
            continue
        logged_ended.add(entry.draft_id)
        if entry.kind == "deleted":
            drafts_deleted.append(DraftDeleted(aid, clean_draft(entry.draft)))
            continue
        sent = a.messages.get(entry.sent_message_id or "")
        if sent is not None:
            claimed.add(sent.id)
        drafts_sent.append(
            DraftSent(aid, clean_draft(entry.draft), clean_message(sent) if sent else {})
        )

    # 2) 기록에 없는 변경(기록 없이 상태를 직접 바꾼 경우)은 기존 추론을 쓴다.
    for did, draft in sorted(b.drafts.items()):
        if did in a.drafts or did in logged_ended:
            continue
        candidates = [
            m
            for mid, m in sorted(new_messages.items())
            if mid not in claimed and "SENT" in m.labels and m.thread_id == draft.thread_id
        ]
        # 기록이 없으면 후보 수와 상관없이 제목·본문이 같은 메시지만 짝으로 본다.
        candidates = [m for m in candidates if _same_content(draft, m)]
        if candidates:
            claimed.add(candidates[0].id)
            drafts_sent.append(DraftSent(aid, clean_draft(draft), clean_message(candidates[0])))
        else:
            drafts_deleted.append(DraftDeleted(aid, clean_draft(draft)))

    for did, draft in a.drafts.items():
        if did not in b.drafts and did not in logged_created:
            drafts_created.append(DraftCreated(aid, clean_draft(draft)))

    for mid, msg in new_messages.items():
        if mid not in claimed:
            messages_added.append(MessageAdded(aid, clean_message(msg)))
