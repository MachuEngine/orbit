"""종단 과제 생성기가 읽는 세계: 뼈대(`skeleton.json`)의 시나리오 기록과 `world.json`의 아티팩트.

정답은 이 세계에서 다시 계산한다. 시나리오 기록(`truth`)은 어느 아티팩트를 가리키는지의 안내이고,
일정·메일·문서의 존재와 시각은 `world.json` 값이 기준이다. 둘이 어긋나면 `TaskWorldError`다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from orbit.connectors.contracts import CalendarEvent
from orbit.evaldata.v1.skeleton import ArtifactRef, ScenarioRecord, Skeleton

SKELETON_FILE = "skeleton.json"
WORLD_FILE = "world.json"


class TaskWorldError(ValueError):
    """뼈대와 세계가 맞지 않거나 시나리오가 가리키는 아티팩트가 세계에 없다."""


@dataclass(frozen=True)
class AccountView:
    """계정 하나. 일정은 계약 모델이고 메일·문서는 `world.json`의 dict 그대로다."""

    account_id: str
    email: str
    name: str
    time_zone: str
    purpose: str
    calendars: tuple[dict[str, Any], ...]
    events: tuple[CalendarEvent, ...]
    messages: dict[str, dict[str, Any]]
    files: dict[str, dict[str, Any]]
    _by_key: dict[tuple[str, str], CalendarEvent] = field(default_factory=dict, repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)

    @property
    def primary_calendar_id(self) -> str:
        for c in self.calendars:
            if c.get("primary"):
                return str(c["id"])
        return str(self.calendars[0]["id"])

    def event(self, calendar_id: str, event_id: str) -> CalendarEvent | None:
        return self._by_key.get((calendar_id, event_id))

    def event_of(self, ref: ArtifactRef) -> CalendarEvent:
        found = self.event(ref.calendar_id or "", ref.id)
        if found is None:
            raise TaskWorldError(f"{self.account_id}: 일정 {ref.id}가 세계에 없다")
        return found

    def message_of(self, ref: ArtifactRef) -> dict[str, Any]:
        found = self.messages.get(ref.id)
        if found is None:
            raise TaskWorldError(f"{self.account_id}: 메일 {ref.id}가 세계에 없다")
        return found

    def file_of(self, ref: ArtifactRef) -> dict[str, Any]:
        found = self.files.get(ref.id)
        if found is None:
            raise TaskWorldError(f"{self.account_id}: 문서 {ref.id}가 세계에 없다")
        return found

    def local_day_bounds(self, day: date) -> tuple[datetime, datetime]:
        tz = self.tz
        return (
            datetime.combine(day, time.min, tzinfo=tz),
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz),
        )

    def events_on_day(self, day: date, *, cancelled: bool = False) -> list[CalendarEvent]:
        """현지 날짜 `day`와 겹치는 일정(시작 순). `cancelled`면 취소된 일정만 돌려준다."""
        tz = self.tz
        out: list[CalendarEvent] = []
        for e in self.events:
            if (e.status == "cancelled") != cancelled:
                continue
            s, en = e.start.astimezone(tz), e.end.astimezone(tz)
            last = (en - timedelta(microseconds=1)).date() if en > s else s.date()
            if s.date() <= day <= last:
                out.append(e)
        return sorted(out, key=lambda e: (e.start, e.end, e.title, e.calendar_id, e.id))

    def same_title_on_day(self, title: str, day: date) -> list[CalendarEvent]:
        """현지 날짜에 시작하는 확정 일정 중 제목이 같은 것."""
        tz = self.tz
        return [
            e
            for e in self.events
            if e.status == "confirmed" and e.title == title and e.start.astimezone(tz).date() == day
        ]

    def upcoming_with(self, email: str, after: datetime) -> list[CalendarEvent]:
        """`after` 이후 시작하는 확정 일정 중 참석자에 `email`이 있는 것(시작 순)."""
        mail = email.casefold()
        found = [
            e
            for e in self.events
            if e.status == "confirmed"
            and e.start > after
            and any(a.email.casefold() == mail for a in e.attendees)
        ]
        return sorted(found, key=lambda e: (e.start, e.id))


@dataclass(frozen=True)
class TaskWorld:
    skeleton: Skeleton
    accounts: dict[str, AccountView]
    skeleton_sha256: str = ""
    world_sha256: str = ""

    def account(self, account_id: str) -> AccountView:
        return self.accounts[account_id]

    def scenarios(self, type_: str) -> list[ScenarioRecord]:
        return [s for s in self.skeleton.scenarios if s.type == type_]


def _view(account_id: str, sk_acct: Any, raw: dict[str, Any]) -> AccountView:
    events = tuple(CalendarEvent.model_validate(e) for e in raw.get("events", []))
    view = AccountView(
        account_id=account_id,
        email=str(raw["email"]),
        name=str(sk_acct.name),
        time_zone=str(sk_acct.time_zone),
        purpose=str(sk_acct.purpose),
        calendars=tuple(raw.get("calendars", [])),
        events=events,
        messages={str(m["id"]): m for m in raw.get("messages", [])},
        files={str(f["id"]): f for f in raw.get("files", [])},
        _by_key={(e.calendar_id, e.id): e for e in events},
    )
    skeleton_events = {(e.calendar_id, e.id) for e in sk_acct.events}
    skeleton_messages = {m.id for m in sk_acct.messages}
    skeleton_files = {f.id for f in sk_acct.files}
    if (
        skeleton_events != set(view._by_key)
        or skeleton_messages != set(view.messages)
        or skeleton_files != set(view.files)
    ):
        raise TaskWorldError(f"{account_id}: skeleton.json과 world.json의 아티팩트가 다르다")
    return view


def build_task_world(
    skeleton: Skeleton,
    world: dict[str, Any],
    *,
    skeleton_sha256: str = "",
    world_sha256: str = "",
) -> TaskWorld:
    raw_accounts = world.get("accounts", {})
    if set(raw_accounts) != set(skeleton.accounts):
        raise TaskWorldError("skeleton.json과 world.json의 계정 목록이 다르다")
    accounts = {
        acct: _view(acct, skeleton.accounts[acct], raw_accounts[acct])
        for acct in sorted(skeleton.accounts)
    }
    return TaskWorld(skeleton, accounts, skeleton_sha256, world_sha256)


def load_task_world(dataset_dir: str | Path) -> tuple[TaskWorld, dict[str, Any]]:
    """데이터셋 디렉터리의 `skeleton.json`·`world.json`을 읽는다. (세계, world dict)를 돌려준다."""
    root = Path(dataset_dir)
    try:
        skeleton_raw = (root / SKELETON_FILE).read_bytes()
        world_raw = (root / WORLD_FILE).read_bytes()
    except OSError as exc:
        raise TaskWorldError(f"{SKELETON_FILE}·{WORLD_FILE}을 읽을 수 없다: {exc}") from exc
    skeleton = Skeleton.model_validate_json(skeleton_raw)
    world: dict[str, Any] = json.loads(world_raw)
    tw = build_task_world(
        skeleton,
        world,
        skeleton_sha256=hashlib.sha256(skeleton_raw).hexdigest(),
        world_sha256=hashlib.sha256(world_raw).hexdigest(),
    )
    return tw, world
