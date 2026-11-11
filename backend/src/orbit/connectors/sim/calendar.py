"""Sim 캘린더 커넥터 (ADR-0008 3절). 계정 하나에 묶이며 모든 공개 메서드는 `SimRuntime`을 거친다.

계약 아님 (Sim 고유 동작, 호출자가 의존하면 안 된다. 실제 어댑터는 다르게 동작할 수 있다):
- 취소된 일정에도 `update_event`가 성공한다.
- 이미 취소된 일정의 재취소가 `if_match`가 맞으면 변경 없이 성공한다(멱등).
- `list_events`의 `query`는 제목·설명·장소의 대소문자 무시 부분 문자열 일치다.
계약이 보장하는 규칙(종일 일정, `ValueError` 조건)은 `CalendarConnector` docstring을 본다.

판단 기록:
- `query`: Sim은 제목·설명·장소에 대한 대소문자 무시 부분 문자열 일치다. 계약은 "제목의
  완전한 단어를 대소문자 무관하게 찾는다"까지만 보장한다(Google `q`는 단어 단위 전문 검색).
  부분 문자열·여러 단어·필드 범위는 Sim 고유 동작이므로 호출자가 의존하면 안 된다.
- 취소된 일정에도 `update_event`를 허용한다(의도한 동작). status는 `cancelled`로 유지되고
  version·`updated_at`은 바뀐다.
- `list_events`는 겹침 기준이다. `time_min <= start < time_max`만 보면 기간 직전에 시작해
  기간 안에서 끝나는 일정이 빠진다. Google `events.list`도 겹침 기준이다. 길이 0인 일정은
  `time_min <= start < time_max`일 때 포함한다.
- 이미 취소된 일정을 다시 취소하면 `if_match`가 현재 버전과 같을 때 변경 없이 성공한다(멱등).
  버전이 다르면 `PreconditionFailed`다. 이때 version·updated_at은 바뀌지 않는다.

계약 해석 규칙 (메인 확정):
- 종일 일정: `all_day=True`면 `start`·`end`는 UTC 기준 00:00이어야 하고 `end`는 배타적이다
  (하루짜리면 end = start + 1일). 시각 성분이 00:00이 아니면 `ValueError`.
  v0 한계: 캘린더 `time_zone`은 쓰지 않고 항상 UTC 날짜 경계로 본다.
- 잘못된 `event_id` 형식은 호출자 버그이므로 `ValueError`다. 검증은 `run_tool` 안에서 하므로
  장애 주입 뒤에 일어나고 호출 기록에 오류로 남는다.
- `update_event`는 병합 결과가 `start < end`가 아니면 `ValueError`(종일이면 날짜 기준)이며
  버전·저장 상태를 바꾸지 않는다. `create_event`에도 같은 검사를 적용한다.
- `list_events`는 naive `time_min`/`time_max`와 `time_min >= time_max`에 `ValueError`를 던진다.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, time
from typing import Any

from orbit.connectors.contracts import (
    CALENDAR_TOOLS,
    AlreadyExists,
    Calendar,
    CalendarEvent,
    EventDraft,
    EventPatch,
    NotFound,
    PreconditionFailed,
    validate_client_event_id,
)
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import AccountState, SimWorld


def _require_aware(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}는 시간대 정보가 있어야 한다")
    return value.astimezone(UTC)


def _check_times(start: datetime, end: datetime, all_day: bool) -> None:
    s, e = start.astimezone(UTC), end.astimezone(UTC)
    if all_day:
        for name, value in (("start", s), ("end", e)):
            if value.time() != time(0, 0):
                raise ValueError(f"종일 일정의 {name}는 UTC 00:00이어야 한다")
    if not s < e:
        raise ValueError("end는 start보다 뒤여야 한다")


class SimCalendarConnector:
    def __init__(self, world: SimWorld, account_id: str, runtime: SimRuntime | None = None) -> None:
        self._world = world
        self._account_id = account_id
        self._rt = runtime if runtime is not None else SimRuntime()
        self._world.account(account_id)  # 없는 계정이면 바로 실패한다
        self._rt.bind(CALENDAR_TOOLS)

    def _run[T](
        self, tool: str, args: Mapping[str, Any], apply_fn: Callable[[], T]
    ) -> Awaitable[T]:
        return self._rt.run_tool(tool, args, apply_fn, account_id=self._account_id)

    @property
    def runtime(self) -> SimRuntime:
        return self._rt

    @property
    def _account(self) -> AccountState:
        return self._world.account(self._account_id)

    def _events_of(self, calendar_id: str) -> dict[str, CalendarEvent]:
        acct = self._account
        if calendar_id not in acct.calendars:
            raise NotFound(f"캘린더 {calendar_id}가 없다")
        return acct.events.setdefault(calendar_id, {})

    def _get(self, calendar_id: str, event_id: str) -> CalendarEvent:
        try:
            return self._events_of(calendar_id)[event_id]
        except KeyError:
            raise NotFound(f"일정 {event_id}가 없다") from None

    def _store(self, event: CalendarEvent) -> CalendarEvent:
        self._events_of(event.calendar_id)[event.id] = event
        return event

    async def list_calendars(self) -> list[Calendar]:
        return await self._run("list_calendars", {}, lambda: list(self._account.calendars.values()))

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        query: str | None = None,
        include_cancelled: bool = False,
    ) -> list[CalendarEvent]:
        args = {
            "calendar_id": calendar_id,
            "time_min": time_min,
            "time_max": time_max,
            "query": query,
            "include_cancelled": include_cancelled,
        }

        def apply() -> list[CalendarEvent]:
            lo = _require_aware("time_min", time_min)
            hi = _require_aware("time_max", time_max)
            if lo >= hi:
                raise ValueError("time_min은 time_max보다 앞서야 한다")
            needle = query.casefold() if query else None
            found: list[CalendarEvent] = []
            for ev in self._events_of(calendar_id).values():
                if ev.status == "cancelled" and not include_cancelled:
                    continue
                start, end = ev.start.astimezone(UTC), ev.end.astimezone(UTC)
                if not start < hi:
                    continue
                if not (end > lo or (start == end and start >= lo)):
                    continue
                if needle is not None and not any(
                    needle in text.casefold() for text in (ev.title, ev.description, ev.location)
                ):
                    continue
                found.append(ev)
            found.sort(key=lambda e: (e.start.astimezone(UTC), e.id))
            return found

        return await self._run("list_events", args, apply)

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        args = {"calendar_id": calendar_id, "event_id": event_id}
        return await self._run("get_event", args, lambda: self._get(calendar_id, event_id))

    async def create_event(
        self, calendar_id: str, event_id: str, draft: EventDraft
    ) -> CalendarEvent:
        args = {"calendar_id": calendar_id, "event_id": event_id, "draft": draft}

        def apply() -> CalendarEvent:
            validate_client_event_id(event_id)
            _check_times(draft.start, draft.end, draft.all_day)
            events = self._events_of(calendar_id)
            if event_id in events:  # 취소된 일정도 존재로 본다
                raise AlreadyExists(f"일정 {event_id}가 이미 있다")
            event = CalendarEvent(
                id=event_id,
                calendar_id=calendar_id,
                version=self._world.ids.version(),
                updated_at=self._world.clock.now(),
                **draft.model_dump(),
            )
            return self._store(event)

        return await self._run("create_event", args, apply)

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch, *, if_match: str
    ) -> CalendarEvent:
        args = {
            "calendar_id": calendar_id,
            "event_id": event_id,
            "patch": patch,
            "if_match": if_match,
        }

        def apply() -> CalendarEvent:
            current = self._get(calendar_id, event_id)
            if current.version != if_match:
                raise PreconditionFailed(f"일정 {event_id}의 버전이 다르다")
            merged = current.model_copy(update=patch.changes())
            _check_times(merged.start, merged.end, merged.all_day)
            updated = merged.model_copy(
                update={"version": self._world.ids.version(), "updated_at": self._world.clock.now()}
            )
            return self._store(updated)

        return await self._run("update_event", args, apply)

    async def cancel_event(
        self, calendar_id: str, event_id: str, *, if_match: str
    ) -> CalendarEvent:
        args = {"calendar_id": calendar_id, "event_id": event_id, "if_match": if_match}

        def apply() -> CalendarEvent:
            current = self._get(calendar_id, event_id)
            if current.version != if_match:
                raise PreconditionFailed(f"일정 {event_id}의 버전이 다르다")
            if current.status == "cancelled":
                return current  # 멱등: 변경 없음
            cancelled = current.model_copy(
                update={
                    "status": "cancelled",
                    "version": self._world.ids.version(),
                    "updated_at": self._world.clock.now(),
                }
            )
            return self._store(cancelled)

        return await self._run("cancel_event", args, apply)
