"""Google Calendar API v3의 일부를 상태 있게 흉내 내는 HTTP 모의 서버 (httpx MockTransport).

흉내 내는 범위
- `calendarList.list`, `events.list`(겹침 기준, `q` 단어 일치, `showDeleted`, 페이지 나눔),
  `events.get`,
  `events.insert`(클라이언트 지정 `id`, 중복 409), `events.patch`(`If-Match` 불일치 412, null은
  필드 삭제, 참석자 배열은 통째 교체되고 응답 상태를 보존하지 않는다).
- etag는 쓰기마다 바뀐다. 취소는 `status=cancelled`로 남고 `get`은 200, `list`는 `showDeleted`일
  때만 낸다.
- 모르는 캘린더·일정 404. 잘못된 일정 ID·빈 시간 범위 400. 토큰이 없으면 401.

흉내 내지 않는 범위 (실계정에서 확인한다, docs/connectors/google-contract.md)
- 검색 색인 지연, `q`의 실제 전문 검색 의미(필드·형태소), 취소된 일정이 시작·끝을 생략하는 응답,
  취소된 일정의 재수정·재취소 응답, 반복 일정, 참석자 초대 메일, 한도 초과, 시간대 표기 차이.
"""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx

from orbit.connectors.contracts import CalendarEvent
from orbit.connectors.sim.world import AccountState

_BASE_PATH = "/calendar/v3"
MOCK_BASE_URL = f"https://calendar.mock.invalid{_BASE_PATH}"
_ID = re.compile(r"[a-v0-9]{5,1024}")
_WORD = re.compile(r"\w+")
_GOOGLE_STATUS = {
    "needs_action": "needsAction",
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "tentative",
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _error(status: int, reason: str) -> httpx.Response:
    body = {"error": {"code": status, "errors": [{"reason": reason}], "message": "mock"}}
    return httpx.Response(status, json=body)


def _bounds(raw: dict[str, Any]) -> tuple[datetime, datetime]:
    def one(obj: dict[str, Any]) -> datetime:
        if obj.get("date"):
            return datetime.fromisoformat(obj["date"]).replace(tzinfo=UTC)
        return datetime.fromisoformat(obj["dateTime"].replace("Z", "+00:00"))

    return one(raw["start"]), one(raw["end"])


class GoogleCalendarMock:
    """`httpx.MockTransport(mock.handle)`로 쓴다. 호출 기록은 `requests`에 남는다."""

    def __init__(self, now: datetime) -> None:
        self._clock = now
        self._etag = 0
        self.calendars: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.page_size_cap = 250  # 테스트가 낮추면 페이지 나눔을 확인할 수 있다

    # ---- 시드

    @classmethod
    def from_account(cls, account: AccountState, now: datetime) -> "GoogleCalendarMock":
        mock = cls(now)
        for cal in account.calendars.values():
            mock.calendars[cal.id] = {
                "id": cal.id,
                "summary": cal.name,
                "timeZone": cal.time_zone,
                "primary": cal.primary,
            }
            mock.events[cal.id] = {}
        for cal_id, by_id in account.events.items():
            for ev in by_id.values():
                mock.events[cal_id][ev.id] = mock._from_model(ev)
        return mock

    def _from_model(self, ev: CalendarEvent) -> dict[str, Any]:
        def field(value: datetime) -> dict[str, str]:
            if ev.all_day:
                return {"date": value.astimezone(UTC).date().isoformat()}
            return {"dateTime": _iso(value)}

        raw: dict[str, Any] = {
            "id": ev.id,
            "etag": ev.version,
            "status": ev.status,
            "summary": ev.title,
            "description": ev.description,
            "location": ev.location,
            "start": field(ev.start),
            "end": field(ev.end),
            "updated": _iso(ev.updated_at),
            "attendees": [
                {
                    "email": a.email,
                    "displayName": a.name,
                    "responseStatus": _GOOGLE_STATUS[a.response_status],
                }
                for a in ev.attendees
            ],
        }
        if ev.organizer is not None:
            raw["organizer"] = {"email": ev.organizer.email, "displayName": ev.organizer.name}
        return raw

    def _touch(self, raw: dict[str, Any]) -> None:
        self._etag += 1
        self._clock += timedelta(seconds=1)
        raw["etag"] = f'"mock-{self._etag}"'
        raw["updated"] = _iso(self._clock)

    # ---- 요청 처리

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return _error(401, "authError")
        path = unquote(request.url.path)
        if not path.startswith(_BASE_PATH):
            return _error(404, "notFound")
        parts = [p for p in path[len(_BASE_PATH) :].split("/") if p]
        method = request.method
        if parts == ["users", "me", "calendarList"] and method == "GET":
            return httpx.Response(200, json={"items": list(self.calendars.values())})
        if len(parts) >= 3 and parts[0] == "calendars" and parts[2] == "events":
            cal_id = parts[1]
            if cal_id not in self.calendars:
                return _error(404, "notFound")
            store = self.events[cal_id]
            if len(parts) == 3 and method == "GET":
                return self._list(request, store)
            if len(parts) == 3 and method == "POST":
                return self._insert(request, store)
            if len(parts) == 4:
                raw = store.get(parts[3])
                if raw is None:
                    return _error(404, "notFound")
                if method == "GET":
                    return httpx.Response(200, json=raw)
                if method == "PATCH":
                    return self._patch(request, raw)
        return _error(404, "notFound")

    def _list(self, request: httpx.Request, store: dict[str, dict[str, Any]]) -> httpx.Response:
        q = request.url.params
        lo = datetime.fromisoformat(q["timeMin"].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(q["timeMax"].replace("Z", "+00:00"))
        show_deleted = q.get("showDeleted") == "true"
        words = {w.casefold() for w in _WORD.findall(q["q"])} if q.get("q") else set()
        matched: list[tuple[datetime, str, dict[str, Any]]] = []
        for raw in store.values():
            if raw["status"] == "cancelled" and not show_deleted:
                continue
            start, end = _bounds(raw)
            if not start < hi or not (end > lo or (start == end and start >= lo)):
                continue
            if words:
                text = " ".join(str(raw.get(k, "")) for k in ("summary", "description", "location"))
                if not words <= {w.casefold() for w in _WORD.findall(text)}:
                    continue
            matched.append((start, raw["id"], raw))
        matched.sort(key=lambda t: (t[0], t[1]))
        size = min(int(q.get("maxResults", "250")), self.page_size_cap)
        offset = int(q.get("pageToken", "0"))
        page = [raw for _, _, raw in matched[offset : offset + size]]
        body: dict[str, Any] = {"items": page}
        if offset + size < len(matched):
            body["nextPageToken"] = str(offset + size)
        return httpx.Response(200, json=body)

    def _insert(self, request: httpx.Request, store: dict[str, dict[str, Any]]) -> httpx.Response:
        body = json.loads(request.content)
        event_id = str(body.get("id", ""))
        if not _ID.fullmatch(event_id):
            return _error(400, "invalid")
        if event_id in store:  # 취소된 일정도 ID를 계속 쓴다
            return _error(409, "duplicate")
        raw = {"status": "confirmed", **body}
        raw["organizer"] = {"email": "owner@example.com"}
        _normalize_attendees(raw)
        if not _valid_range(raw):
            return _error(400, "timeRangeEmpty")
        self._touch(raw)
        store[event_id] = raw
        return httpx.Response(200, json=raw)

    def _patch(self, request: httpx.Request, raw: dict[str, Any]) -> httpx.Response:
        if_match = request.headers.get("If-Match")
        if if_match is not None and if_match != raw["etag"]:
            return _error(412, "conditionNotMet")
        merged = dict(raw)
        for key, value in json.loads(request.content).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {k: v for k, v in {**merged[key], **value}.items() if v is not None}
            elif value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        _normalize_attendees(merged)
        if not _valid_range(merged):
            return _error(400, "timeRangeEmpty")
        raw.clear()
        raw.update(merged)
        self._touch(raw)
        return httpx.Response(200, json=raw)


def _normalize_attendees(raw: dict[str, Any]) -> None:
    """실제 Google처럼 배열을 보낸 그대로 저장한다. 상태를 안 보낸 참석자는 needsAction이다."""
    if "attendees" in raw:
        raw["attendees"] = [
            {**a, "responseStatus": a.get("responseStatus", "needsAction")}
            for a in raw["attendees"]
        ]


def _valid_range(raw: dict[str, Any]) -> bool:
    try:
        start, end = _bounds(raw)
    except (KeyError, ValueError):
        return False
    return start < end


def mock_transport(mock: GoogleCalendarMock) -> httpx.MockTransport:
    handler: Callable[[httpx.Request], httpx.Response] = mock.handle
    return httpx.MockTransport(handler)
