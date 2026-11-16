"""Google Calendar Adapter (ADR-0016 4절, ADR-0008 3절). Calendar API v3 REST, httpx 비동기.

동작 규칙
- 일정 ID는 클라이언트가 정한다(`id` 필드). 409는 `AlreadyExists`다.
- `version`은 Google의 `etag`다. 수정·취소는 `If-Match: <etag>`를 보내고 412는
  `PreconditionFailed`다.
- 취소는 `status=cancelled` PATCH다(삭제 아님).
- 401이면 토큰을 한 번 새로 받아 같은 요청을 다시 보낸다. 새로 받지 못하면 `PermissionRevoked`다.
  401은 요청이 적용되지 않았다는 뜻이므로 쓰기도 다시 보내도 된다.
- 읽기만 짧게 자동 재시도한다(5xx, 전송 오류, 짧은 `Retry-After`의 429).
- 쓰기는 요청이 나간 뒤의 타임아웃·연결 끊김·5xx·읽을 수 없는 응답을 `ResultUnknown`으로 낸다.
  요청이 나가기 전의 실패(연결 실패, 토큰 공급 실패)는 `TransientError`다. 쓰기는 재시도하지 않는다.
- 오류 메시지에는 HTTP 상태와 Google 이유 코드(영숫자)만 넣는다. 일정 내용·토큰·응답 본문은 넣지
  않는다.

판단 기록
- `sendUpdates=none`: 참석자에게 초대·변경 메일을 Google이 자동으로 보내지 않게 한다. 사용자 승인
  없는
  외부 발송 경로를 만들지 않기 위해서다(CLAUDE.md). 초대 메일을 보내는 동작은 별도 결정이 필요하다.
- 종일 일정(ADR-0027): Google `date`(끝 배타적)와 계약의 "캘린더 시간대 00:00인 순간"을 서로
  바꾼다. 캘린더 시간대는 `events.list` 응답의 최상위 `timeZone`, 없으면 `calendarList` 값,
  그것도 없으면 `calendars.get`(캘린더 메타) 순으로 쓰고, 모두 없으면 UTC다. 시간대는 연결
  인스턴스 안에서 캘린더별로 기억한다. 쓸 때는 `start`·`end`를 캘린더 시간대 날짜로 바꿔 `date`로
  보낸다. 종일 일정을 읽거나 쓸 때만 시간대를 조회하므로 일반 일정의 요청 수는 그대로다.
- 수정 패치가 시각·종일 여부를 건드리면 현재 일정을 먼저 읽어 병합 결과를 검증한다(계약의
  `ValueError`).
  이때 `if_match`가 현재 etag와 다르면 `PreconditionFailed`다. 그 밖의 패치는 읽지 않고 바로
  PATCH한다.
- 주최자 응답 상태: Google은 주최자에게 상태를 주지 않는다. 같은 이메일의 참석자가 있으면 그
  상태를 쓰고
  없으면 `accepted`로 둔다(주최자는 자기 일정을 수락한 것으로 본다).
- 취소된 일정은 Google이 시작·끝을 생략할 수 있다. 그때는 `updated_at`을 시작·끝의 자리 값으로 쓴다
  (실계정 확인 항목, docs/connectors/google-contract.md).
- 400은 검색어가 있는 `list_events`에서만 `InvalidQuery`다. 그 밖의 400과 권한 외 403은 기반 클래스
  `ConnectorError`(이유 코드 포함)로 낸다. 계약에 맞는 오류가 없고 `PermissionRevoked`로 매핑하면
  재연결을 요구하는 오해를 만든다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

import httpx

from orbit.connectors.contracts import (
    Attendee,
    Calendar,
    CalendarEvent,
    EventDraft,
    EventPatch,
    PreconditionFailed,
    ResponseStatus,
    ResultUnknown,
    TransientError,
    calendar_zone,
    check_event_times,
    local_date,
    local_midnight,
    validate_client_event_id,
)
from orbit.connectors.google._http import GoogleHttp, Sleep
from orbit.connectors.google.tokens import AccessTokenProvider

DEFAULT_BASE_URL = "https://www.googleapis.com/calendar/v3"

_STATUS_FROM_GOOGLE: dict[str, ResponseStatus] = {
    "needsAction": "needs_action",
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "tentative",
}
_PAGE_SIZE = 250
_MAX_PAGES = 40  # 끝없는 순회 방지(최대 10,000건)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------- 시각 변환


def _require_aware(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}는 시간대 정보가 있어야 한다")
    return value.astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _time_field(value: datetime, all_day: bool, time_zone: str) -> dict[str, str | None]:
    """Google의 `start`/`end` 객체. 쓰지 않는 쪽은 null로 보내 PATCH에서 지운다.

    종일 일정은 캘린더 시간대의 날짜를 `date`로 보낸다(ADR-0027).
    """
    if all_day:
        return {"date": local_date(value, time_zone).isoformat(), "dateTime": None}
    return {"dateTime": _rfc3339(value), "date": None}


def _checked_zone(value: Any) -> str:
    """응답의 시간대 이름 검증. 모르는 이름은 읽을 수 없는 응답이므로 `TransientError`다."""
    name = str(value or "UTC")
    try:
        calendar_zone(name)
    except ValueError:
        raise TransientError("Google 응답의 캘린더 시간대를 알 수 없다") from None
    return name


def _has_date(raw: Mapping[str, Any]) -> bool:
    start = raw.get("start")
    return isinstance(start, Mapping) and bool(start.get("date"))


def _parse_time(obj: Any, time_zone: str) -> tuple[datetime, bool] | None:
    if not isinstance(obj, Mapping):
        return None
    if obj.get("date"):
        day = date.fromisoformat(str(obj["date"]))
        return local_midnight(day, time_zone), True
    if obj.get("dateTime"):
        parsed = datetime.fromisoformat(str(obj["dateTime"]))
        if parsed.tzinfo is None:  # Google은 offset을 주지만 없으면 UTC로 본다
            parsed = parsed.replace(tzinfo=UTC)
        return parsed, False
    return None


# ---------------------------------------------------------------- 응답 변환


def _attendee(raw: Mapping[str, Any]) -> Attendee:
    return Attendee(
        email=str(raw.get("email", "")),
        name=raw.get("displayName"),
        response_status=_STATUS_FROM_GOOGLE.get(str(raw.get("responseStatus")), "needs_action"),
    )


def _event_from_google(
    calendar_id: str, raw: Mapping[str, Any], time_zone: str = "UTC"
) -> CalendarEvent:
    updated_raw = raw.get("updated")
    updated = datetime.fromisoformat(str(updated_raw)) if updated_raw else _EPOCH
    status = "cancelled" if raw.get("status") == "cancelled" else "confirmed"
    start = _parse_time(raw.get("start"), time_zone)
    end = _parse_time(raw.get("end"), time_zone)
    if start is None or end is None:
        if status != "cancelled":
            raise TransientError("Google 응답에 일정 시각이 없다")
        start = end = (updated, False)
    attendees = tuple(_attendee(a) for a in raw.get("attendees", ()))
    organizer: Attendee | None = None
    org = raw.get("organizer")
    if isinstance(org, Mapping):
        email = str(org.get("email", ""))
        known = next((a for a in attendees if a.email.casefold() == email.casefold()), None)
        organizer = Attendee(
            email=email,
            name=org.get("displayName"),
            response_status=known.response_status if known else "accepted",
        )
    return CalendarEvent(
        id=str(raw["id"]),
        calendar_id=calendar_id,
        title=str(raw.get("summary", "")),
        description=str(raw.get("description", "")),
        location=str(raw.get("location", "")),
        start=start[0],
        end=end[0],
        all_day=start[1],
        attendees=attendees,
        organizer=organizer,
        status=status,
        version=str(raw.get("etag", "")),
        updated_at=updated,
    )


_STATUS_TO_GOOGLE = {v: k for k, v in _STATUS_FROM_GOOGLE.items()}


def _attendees_body(
    attendees: tuple[Attendee, ...], current: tuple[Attendee, ...] = ()
) -> list[dict[str, str]]:
    """참석자 배열. PATCH는 배열을 통째로 교체하므로, 현재 일정에 있던 같은 이메일 참석자의
    응답 상태는 그대로 실어 보낸다(새 참석자는 상태를 생략한다)."""
    known = {a.email.casefold(): a for a in current}
    body: list[dict[str, str]] = []
    for a in attendees:
        item = {"email": a.email}
        if a.name:
            item["displayName"] = a.name
        prior = known.get(a.email.casefold())
        if prior is not None:
            item["responseStatus"] = _STATUS_TO_GOOGLE[prior.response_status]
        body.append(item)
    return body


# ---------------------------------------------------------------- 커넥터


class GoogleCalendarConnector:
    """계정 하나에 묶인 `CalendarConnector` 구현. 토큰은 `AccessTokenProvider`에서 받는다.

    `base_url`은 `https://*.googleapis.com`만 허용한다. 테스트에서만 `allow_test_base_url=True`로
    풀 수 있고, `client` 주입도 그때만 가능하다(그 클라이언트의 `base_url`을 따른다).
    """

    def __init__(
        self,
        tokens: AccessTokenProvider,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        allow_test_base_url: bool = False,
        timeout_s: float = 15.0,
        read_retries: int = 2,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        # calendar_id -> IANA 시간대 이름. 연결(요청) 단위로 만드는 짧은 수명의 인스턴스를 전제로 한
        # 캐시다. 오래 사는 인스턴스라면 캘린더 시간대 변경이 반영되지 않는다. 쓰기 경로는 캐시가
        # 없을 때 `calendars.get`으로 가져온다.
        self._zones: dict[str, str] = {}
        self._http = GoogleHttp(
            tokens,
            service="Google Calendar",
            base_url=base_url,
            client=client,
            allow_test_base_url=allow_test_base_url,
            timeout_s=timeout_s,
            read_retries=read_retries,
            sleep=sleep,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        op: str,
        write: bool,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        if_match: str | None = None,
        creating: bool = False,
        query: bool = False,
    ) -> dict[str, Any]:
        return await self._http.request(
            method,
            path,
            op=op,
            write=write,
            params=params,
            body=body,
            extra_headers={"If-Match": if_match} if if_match is not None else None,
            creating=creating,
            query=query,
        )

    @staticmethod
    def _cal(calendar_id: str) -> str:
        return f"/calendars/{quote(calendar_id, safe='')}"

    def _ev(self, calendar_id: str, event_id: str) -> str:
        return f"{self._cal(calendar_id)}/events/{quote(event_id, safe='')}"

    # ---- 캘린더 시간대 (ADR-0027)

    async def _zone(self, calendar_id: str) -> str:
        """캘린더 시간대 이름. 기억한 값이 없으면 `calendars.get`으로 조회한다(없으면 UTC)."""
        known = self._zones.get(calendar_id)
        if known is not None:
            return known
        data = await self._request("GET", self._cal(calendar_id), op="get_calendar", write=False)
        name = _checked_zone(data.get("timeZone"))
        self._zones[calendar_id] = name
        return name

    async def _event(self, calendar_id: str, raw: Mapping[str, Any]) -> CalendarEvent:
        """응답 변환. 종일 일정일 때만 시간대가 필요하다."""
        if not _has_date(raw):
            return _event_from_google(calendar_id, raw)
        return _event_from_google(calendar_id, raw, await self._zone(calendar_id))

    async def _written(self, calendar_id: str, data: Mapping[str, Any], op: str) -> CalendarEvent:
        """쓰기 성공 응답의 변환. 쓰기는 이미 적용됐으므로 변환 실패는 모두 `ResultUnknown`이다."""
        try:
            return await self._event(calendar_id, data)
        except Exception:
            raise ResultUnknown(f"Google Calendar {op} 응답을 해석하지 못했다") from None

    # ---- 계약

    async def list_calendars(self) -> list[Calendar]:
        calendars: list[Calendar] = []
        page: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"maxResults": 250}
            if page:
                params["pageToken"] = page
            data = await self._request(
                "GET", "/users/me/calendarList", op="list_calendars", write=False, params=params
            )
            for item in data.get("items", ()):
                calendar = Calendar(
                    id=str(item["id"]),
                    name=str(item.get("summaryOverride") or item.get("summary") or ""),
                    time_zone=str(item.get("timeZone") or "UTC"),
                    primary=bool(item.get("primary", False)),
                )
                self._zones[calendar.id] = calendar.time_zone
                calendars.append(calendar)
            page = data.get("nextPageToken")
            if not page:
                break
        return calendars

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        query: str | None = None,
        include_cancelled: bool = False,
    ) -> list[CalendarEvent]:
        lo = _require_aware("time_min", time_min)
        hi = _require_aware("time_max", time_max)
        if lo >= hi:
            raise ValueError("time_min은 time_max보다 앞서야 한다")
        found: list[CalendarEvent] = []
        page: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "timeMin": _rfc3339(lo),
                "timeMax": _rfc3339(hi),
                "singleEvents": "true",
                "orderBy": "startTime",
                "showDeleted": "true" if include_cancelled else "false",
                "maxResults": _PAGE_SIZE,
            }
            if query:
                params["q"] = query
            if page:
                params["pageToken"] = page
            data = await self._request(
                "GET",
                f"{self._cal(calendar_id)}/events",
                op="list_events",
                write=False,
                params=params,
                query=bool(query),
            )
            if data.get("timeZone"):
                self._zones[calendar_id] = _checked_zone(data["timeZone"])
            for raw in data.get("items", ()):
                event = await self._event(calendar_id, raw)
                if event.status == "cancelled" and not include_cancelled:
                    continue
                found.append(event)
            page = data.get("nextPageToken")
            if not page:
                break
        found.sort(key=lambda e: (e.start.astimezone(UTC), e.id))
        return found

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        data = await self._request(
            "GET", self._ev(calendar_id, event_id), op="get_event", write=False
        )
        return await self._event(calendar_id, data)

    async def create_event(
        self, calendar_id: str, event_id: str, draft: EventDraft
    ) -> CalendarEvent:
        validate_client_event_id(event_id)
        zone = await self._zone(calendar_id) if draft.all_day else "UTC"
        check_event_times(draft.start, draft.end, draft.all_day, zone)
        body: dict[str, Any] = {
            "id": event_id,
            "summary": draft.title,
            "description": draft.description,
            "location": draft.location,
            "start": _time_field(draft.start, draft.all_day, zone),
            "end": _time_field(draft.end, draft.all_day, zone),
        }
        if draft.attendees:
            body["attendees"] = _attendees_body(draft.attendees)
        data = await self._request(
            "POST",
            f"{self._cal(calendar_id)}/events",
            op="create_event",
            write=True,
            params={"sendUpdates": "none"},
            body=body,
            creating=True,
        )
        return await self._written(calendar_id, data, "create_event")

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch, *, if_match: str
    ) -> CalendarEvent:
        body: dict[str, Any] = {}
        if patch.title is not None:
            body["summary"] = patch.title
        if patch.description is not None:
            body["description"] = patch.description
        if patch.location is not None:
            body["location"] = patch.location
        touches_time = patch.start is not None or patch.end is not None or patch.all_day is not None
        if touches_time or patch.attendees is not None:
            current = await self.get_event(calendar_id, event_id)
            if current.version != if_match:
                raise PreconditionFailed("Google Calendar update_event 실패: etag가 다르다")
            if patch.attendees is not None:
                body["attendees"] = _attendees_body(patch.attendees, current.attendees)
        if touches_time:
            start = patch.start if patch.start is not None else current.start
            end = patch.end if patch.end is not None else current.end
            all_day = patch.all_day if patch.all_day is not None else current.all_day
            zone = await self._zone(calendar_id) if all_day else "UTC"
            check_event_times(start, end, all_day, zone)
            body["start"] = _time_field(start, all_day, zone)
            body["end"] = _time_field(end, all_day, zone)
        data = await self._request(
            "PATCH",
            self._ev(calendar_id, event_id),
            op="update_event",
            write=True,
            params={"sendUpdates": "none"},
            body=body,
            if_match=if_match,
        )
        return await self._written(calendar_id, data, "update_event")

    async def cancel_event(
        self, calendar_id: str, event_id: str, *, if_match: str
    ) -> CalendarEvent:
        data = await self._request(
            "PATCH",
            self._ev(calendar_id, event_id),
            op="cancel_event",
            write=True,
            params={"sendUpdates": "none"},
            body={"status": "cancelled"},
            if_match=if_match,
        )
        return await self._written(calendar_id, data, "cancel_event")
