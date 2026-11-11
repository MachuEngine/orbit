"""GoogleCalendarConnector 단위 테스트. HTTP는 모두 `httpx.MockTransport`다(실제 호출 없음)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from orbit.connectors.contracts import (
    AlreadyExists,
    ConnectorError,
    EventDraft,
    EventPatch,
    InvalidQuery,
    NotFound,
    PermissionRevoked,
    PreconditionFailed,
    RateLimited,
    ResultUnknown,
    TransientError,
)
from orbit.connectors.google.calendar import GoogleCalendarConnector

CAL = "primary@example.com"
EV = "evnaa00001"
START = datetime(2026, 11, 20, 8, tzinfo=UTC)
SECRET_TEXT = "합성비밀내용-ZZZ"  # 오류 메시지로 새면 안 되는 값


class Tokens:
    def __init__(self, *, refresh_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._refresh_error = refresh_error

    async def access_token(self) -> str:
        self.calls.append("access")
        return "tok-old"

    async def refresh(self) -> str:
        self.calls.append("refresh")
        if self._refresh_error is not None:
            raise self._refresh_error
        return "tok-new"


Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make(
    handler: Handler, tokens: Tokens | None = None
) -> tuple[GoogleCalendarConnector, Recorder, list[float]]:
    rec = Recorder(handler)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(base_url="https://gcal.test/v3", transport=httpx.MockTransport(rec))
    conn = GoogleCalendarConnector(tokens or Tokens(), client=client, sleep=fake_sleep)
    return conn, rec, sleeps


def error_response(
    status: int, reason: str = "", headers: dict[str, str] | None = None
) -> httpx.Response:
    body = {"error": {"code": status, "errors": [{"reason": reason}], "message": SECRET_TEXT}}
    return httpx.Response(status, json=body, headers=headers)


def event_json(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": EV,
        "etag": '"e1"',
        "status": "confirmed",
        "summary": "회의",
        "start": {"dateTime": "2026-11-20T08:00:00Z"},
        "end": {"dateTime": "2026-11-20T09:00:00Z"},
        "updated": "2026-11-20T00:00:00.000Z",
    }
    base.update(over)
    return base


def draft() -> EventDraft:
    return EventDraft(title="신규", start=START, end=START + timedelta(hours=1))


# ---------------------------------------------------------------- 오류 매핑 표

READ_ERRORS: list[tuple[int, str, type[ConnectorError]]] = [
    (403, "rateLimitExceeded", RateLimited),
    (403, "userRateLimitExceeded", RateLimited),
    (429, "", RateLimited),
    (403, "insufficientPermissions", PermissionRevoked),
    (400, "invalid_grant", PermissionRevoked),
    (404, "notFound", NotFound),
    (410, "deleted", NotFound),
    (403, "forbidden", ConnectorError),
]


@pytest.mark.parametrize(("status", "reason", "expected"), READ_ERRORS)
async def test_read_error_mapping(status: int, reason: str, expected: type[ConnectorError]) -> None:
    conn, _, _ = make(lambda r: error_response(status, reason))
    with pytest.raises(expected) as info:
        await conn.get_event(CAL, EV)
    assert type(info.value) is expected
    assert SECRET_TEXT not in info.value.message
    assert "tok-" not in info.value.message


async def test_oauth_style_invalid_grant_body_is_permission_revoked() -> None:
    conn, _, _ = make(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(PermissionRevoked):
        await conn.get_event(CAL, EV)


async def test_rate_limited_carries_retry_after() -> None:
    conn, _, _ = make(lambda r: error_response(429, "", {"Retry-After": "30"}))
    with pytest.raises(RateLimited) as info:
        await conn.get_event(CAL, EV)
    assert info.value.retry_after_s == 30.0


async def test_write_rate_limit_is_not_retried() -> None:
    conn, rec, _ = make(lambda r: error_response(403, "rateLimitExceeded", {"Retry-After": "1"}))
    with pytest.raises(RateLimited):
        await conn.create_event(CAL, EV, draft())
    assert len(rec.requests) == 1


async def test_create_conflict_is_already_exists() -> None:
    conn, _, _ = make(lambda r: error_response(409, "duplicate"))
    with pytest.raises(AlreadyExists):
        await conn.create_event(CAL, EV, draft())


async def test_patch_and_cancel_412_is_precondition_failed() -> None:
    conn, _, _ = make(lambda r: error_response(412, "conditionNotMet"))
    with pytest.raises(PreconditionFailed):
        await conn.update_event(CAL, EV, EventPatch(title="x"), if_match='"e1"')
    with pytest.raises(PreconditionFailed):
        await conn.cancel_event(CAL, EV, if_match='"e1"')


async def test_search_400_is_invalid_query_only_with_query() -> None:
    conn, _, _ = make(lambda r: error_response(400, "invalid"))
    with pytest.raises(InvalidQuery):
        await conn.list_events(CAL, START, START + timedelta(days=1), query="a")
    with pytest.raises(ConnectorError) as info:
        await conn.list_events(CAL, START, START + timedelta(days=1))
    assert not isinstance(info.value, InvalidQuery)


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_read_5xx_retries_then_transient(status: int) -> None:
    conn, rec, sleeps = make(lambda r: error_response(status, "backendError"))
    with pytest.raises(TransientError):
        await conn.get_event(CAL, EV)
    assert len(rec.requests) == 3  # 첫 시도 + 재시도 2회
    assert sleeps == [0.2, 0.4]


async def test_read_retry_recovers() -> None:
    answers: Any = iter([error_response(503), httpx.ReadTimeout("slow"), None])

    def handler(request: httpx.Request) -> httpx.Response:
        item = next(answers)
        if isinstance(item, Exception):
            raise item
        return item or httpx.Response(200, json=event_json())

    conn, rec, _ = make(handler)
    assert (await conn.get_event(CAL, EV)).id == EV
    assert len(rec.requests) == 3


async def test_read_retries_short_rate_limit_only() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return error_response(429, "", {"Retry-After": "1"})
        return httpx.Response(200, json=event_json())

    conn, _, sleeps = make(handler)
    await conn.get_event(CAL, EV)
    assert sleeps == [1.0]
    conn2, rec2, _ = make(lambda r: error_response(429, "", {"Retry-After": "60"}))
    with pytest.raises(RateLimited):
        await conn2.get_event(CAL, EV)
    assert len(rec2.requests) == 1  # 긴 대기는 호출자에게 넘긴다


async def test_read_connection_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    conn, rec, _ = make(handler)
    with pytest.raises(TransientError):
        await conn.get_event(CAL, EV)
    assert len(rec.requests) == 3


# ---------------------------------------------------------------- 쓰기 결과 불명

WRITES: list[tuple[str, Callable[[GoogleCalendarConnector], Any]]] = [
    ("create", lambda c: c.create_event(CAL, EV, draft())),
    ("update", lambda c: c.update_event(CAL, EV, EventPatch(title="x"), if_match='"e1"')),
    ("cancel", lambda c: c.cancel_event(CAL, EV, if_match='"e1"')),
]


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError])
@pytest.mark.parametrize(("name", "call"), WRITES, ids=[w[0] for w in WRITES])
async def test_write_failure_after_send_is_result_unknown(
    name: str, call: Callable[[GoogleCalendarConnector], Any], exc: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("lost")

    conn, rec, sleeps = make(handler)
    with pytest.raises(ResultUnknown):
        await call(conn)
    assert len(rec.requests) == 1  # 재호출하지 않는다
    assert sleeps == []


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize(("name", "call"), WRITES, ids=[w[0] for w in WRITES])
async def test_write_5xx_is_result_unknown_without_retry(
    name: str, call: Callable[[GoogleCalendarConnector], Any], status: int
) -> None:
    conn, rec, _ = make(lambda r: error_response(status, "backendError"))
    with pytest.raises(ResultUnknown) as info:
        await call(conn)
    assert len(rec.requests) == 1
    assert SECRET_TEXT not in info.value.message


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
@pytest.mark.parametrize(("name", "call"), WRITES, ids=[w[0] for w in WRITES])
async def test_write_failure_before_send_is_transient(
    name: str, call: Callable[[GoogleCalendarConnector], Any], exc: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("no connection")

    conn, rec, _ = make(handler)
    with pytest.raises(TransientError):
        await call(conn)
    assert len(rec.requests) == 1


async def test_write_unreadable_success_body_is_result_unknown() -> None:
    conn, _, _ = make(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(ResultUnknown):
        await conn.create_event(CAL, EV, draft())


async def test_token_supply_failure_is_transient_before_any_request() -> None:
    class Broken(Tokens):
        async def access_token(self) -> str:
            raise OSError("disk")

    conn, rec, _ = make(lambda r: httpx.Response(200, json=event_json()), Broken())
    with pytest.raises(TransientError):
        await conn.create_event(CAL, EV, draft())
    assert rec.requests == []


# ---------------------------------------------------------------- 토큰 갱신


async def test_401_refreshes_once_then_succeeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer tok-old":
            return error_response(401, "authError")
        return httpx.Response(200, json=event_json())

    tokens = Tokens()
    conn, rec, _ = make(handler, tokens)
    assert (await conn.get_event(CAL, EV)).id == EV
    assert tokens.calls == ["access", "refresh"]
    assert [r.headers["Authorization"] for r in rec.requests] == [
        "Bearer tok-old",
        "Bearer tok-new",
    ]


async def test_401_on_write_retries_once_after_refresh() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer tok-old":
            return error_response(401, "authError")
        return httpx.Response(200, json=event_json())

    conn, rec, _ = make(handler)
    await conn.create_event(CAL, EV, draft())
    assert len(rec.requests) == 2


async def test_401_after_refresh_is_permission_revoked() -> None:
    conn, rec, _ = make(lambda r: error_response(401, "authError"))
    with pytest.raises(PermissionRevoked):
        await conn.get_event(CAL, EV)
    assert len(rec.requests) == 2  # 갱신은 한 번뿐이다


@pytest.mark.parametrize(
    "refresh_error", [RuntimeError("invalid_grant"), PermissionRevoked("revoked"), OSError("x")]
)
async def test_refresh_failure_is_permission_revoked(refresh_error: Exception) -> None:
    conn, rec, _ = make(
        lambda r: error_response(401, "authError"), Tokens(refresh_error=refresh_error)
    )
    with pytest.raises(PermissionRevoked):
        await conn.cancel_event(CAL, EV, if_match='"e1"')
    assert len(rec.requests) == 1


# ---------------------------------------------------------------- 요청·변환


async def test_create_sends_client_id_and_suppresses_invites() -> None:
    conn, rec, _ = make(lambda r: httpx.Response(200, json=event_json()))
    await conn.create_event(CAL, EV, draft())
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.raw_path.split(b"?")[0] == b"/v3/calendars/primary%40example.com/events"
    assert req.url.params["sendUpdates"] == "none"
    body = json.loads(req.content)
    assert body["id"] == EV
    assert body["start"] == {"dateTime": "2026-11-20T08:00:00Z", "date": None}


async def test_update_and_cancel_send_if_match() -> None:
    conn, rec, _ = make(lambda r: httpx.Response(200, json=event_json()))
    await conn.update_event(CAL, EV, EventPatch(title="새 제목"), if_match='"e1"')
    await conn.cancel_event(CAL, EV, if_match='"e2"')
    update, cancel = rec.requests
    assert (update.method, update.headers["If-Match"]) == ("PATCH", '"e1"')
    assert json.loads(update.content) == {
        "summary": "새 제목"
    }  # 제목만 바꾸면 읽기 없이 바로 PATCH
    assert (cancel.method, cancel.headers["If-Match"]) == ("PATCH", '"e2"')
    assert json.loads(cancel.content) == {"status": "cancelled"}
    assert len(rec.requests) == 2


async def test_all_day_event_converts_between_date_and_utc_midnight() -> None:
    raw = event_json(start={"date": "2026-11-21"}, end={"date": "2026-11-23"})
    conn, rec, _ = make(lambda r: httpx.Response(200, json=raw))
    ev = await conn.get_event(CAL, EV)
    assert ev.all_day
    assert ev.start == datetime(2026, 11, 21, tzinfo=UTC)
    assert ev.end == datetime(2026, 11, 23, tzinfo=UTC)  # 끝 배타적
    created = await conn.create_event(
        CAL,
        EV,
        EventDraft(
            title="종일",
            start=datetime(2026, 11, 21, tzinfo=UTC),
            end=datetime(2026, 11, 23, tzinfo=UTC),
            all_day=True,
        ),
    )
    assert created.all_day
    body = json.loads(rec.requests[-1].content)
    assert body["start"] == {"date": "2026-11-21", "dateTime": None}
    assert body["end"] == {"date": "2026-11-23", "dateTime": None}


async def test_all_day_rejects_non_midnight_before_sending() -> None:
    conn, rec, _ = make(lambda r: httpx.Response(200, json=event_json()))
    with pytest.raises(ValueError):
        await conn.create_event(
            CAL,
            EV,
            EventDraft(title="x", start=START, end=START + timedelta(days=1), all_day=True),
        )
    assert rec.requests == []


async def test_timezone_offsets_are_parsed_and_sent_as_utc() -> None:
    kst = timezone(timedelta(hours=9))
    raw = event_json(
        start={"dateTime": "2026-11-20T17:00:00+09:00", "timeZone": "Asia/Seoul"},
        end={"dateTime": "2026-11-20T18:00:00+09:00", "timeZone": "Asia/Seoul"},
    )
    conn, rec, _ = make(lambda r: httpx.Response(200, json=raw))
    ev = await conn.get_event(CAL, EV)
    assert ev.start == START
    assert ev.start.utcoffset() == timedelta(hours=9)
    await conn.list_events(
        CAL, datetime(2026, 11, 20, 17, tzinfo=kst), datetime(2026, 11, 21, tzinfo=kst)
    )
    params = rec.requests[-1].url.params
    assert params["timeMin"] == "2026-11-20T08:00:00Z"
    assert params["timeMax"] == "2026-11-20T15:00:00Z"


async def test_event_fields_attendees_organizer_version() -> None:
    raw = event_json(
        description="설명",
        location="장소",
        attendees=[
            {"email": "b@example.org", "displayName": "밥", "responseStatus": "declined"},
            {"email": "own@example.com", "responseStatus": "tentative"},
            {"email": "c@example.org", "responseStatus": "needsAction"},
        ],
        organizer={"email": "own@example.com", "displayName": "주최"},
    )
    conn, _, _ = make(lambda r: httpx.Response(200, json=raw))
    ev = await conn.get_event(CAL, EV)
    assert ev.version == '"e1"'
    assert ev.updated_at == datetime(2026, 11, 20, tzinfo=UTC)
    assert [a.response_status for a in ev.attendees] == ["declined", "tentative", "needs_action"]
    assert ev.organizer is not None
    assert (ev.organizer.name, ev.organizer.response_status) == ("주최", "tentative")


async def test_cancelled_event_without_times_still_parses() -> None:
    raw = {"id": EV, "etag": '"e9"', "status": "cancelled", "updated": "2026-11-20T00:00:00Z"}
    conn, _, _ = make(lambda r: httpx.Response(200, json=raw))
    ev = await conn.get_event(CAL, EV)
    assert ev.status == "cancelled"


async def test_update_time_patch_reads_current_and_checks_etag_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"  # 시각 패치는 먼저 읽는다
        return httpx.Response(200, json=event_json())

    conn, rec, _ = make(handler)
    with pytest.raises(PreconditionFailed):
        await conn.update_event(CAL, EV, EventPatch(all_day=False, title="x"), if_match='"old"')
    assert len(rec.requests) == 1


async def test_update_merge_validation_raises_before_patch() -> None:
    conn, rec, _ = make(lambda r: httpx.Response(200, json=event_json()))
    with pytest.raises(ValueError):
        await conn.update_event(
            CAL, EV, EventPatch(start=START + timedelta(hours=5)), if_match='"e1"'
        )
    assert [r.method for r in rec.requests] == ["GET"]


async def test_list_events_paginates_and_sorts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("pageToken") == "p2":
            return httpx.Response(200, json={"items": [event_json(id="evaa00001")]})
        return httpx.Response(
            200,
            json={
                "items": [
                    event_json(
                        id="evzz00001",
                        start={"dateTime": "2026-11-20T10:00:00Z"},
                        end={"dateTime": "2026-11-20T11:00:00Z"},
                    )
                ],
                "nextPageToken": "p2",
            },
        )

    conn, rec, _ = make(handler)
    events = await conn.list_events(CAL, START - timedelta(days=1), START + timedelta(days=1))
    assert [e.id for e in events] == ["evaa00001", "evzz00001"]
    assert len(rec.requests) == 2


async def test_list_events_drops_cancelled_unless_requested() -> None:
    items = [event_json(), event_json(id="evbb00001", status="cancelled")]
    conn, rec, _ = make(lambda r: httpx.Response(200, json={"items": items}))
    lo, hi = START - timedelta(days=1), START + timedelta(days=1)
    assert [e.id for e in await conn.list_events(CAL, lo, hi)] == [EV]
    assert rec.requests[0].url.params["showDeleted"] == "false"
    both = await conn.list_events(CAL, lo, hi, include_cancelled=True)
    assert len(both) == 2
    assert rec.requests[1].url.params["showDeleted"] == "true"


async def test_list_calendars() -> None:
    items = [
        {
            "id": CAL,
            "summary": "기본",
            "summaryOverride": "내 캘린더",
            "timeZone": "Asia/Seoul",
            "primary": True,
        },
        {"id": "team@example.com", "summary": "팀"},
    ]
    conn, _, _ = make(lambda r: httpx.Response(200, json={"items": items}))
    cals = await conn.list_calendars()
    assert [(c.id, c.name, c.time_zone, c.primary) for c in cals] == [
        (CAL, "내 캘린더", "Asia/Seoul", True),
        ("team@example.com", "팀", "UTC", False),
    ]


async def test_invalid_arguments_raise_value_error_without_request() -> None:
    conn, rec, _ = make(lambda r: httpx.Response(200, json=event_json()))
    naive = datetime(2026, 11, 1)  # noqa: DTZ001  naive 입력 검증용
    with pytest.raises(ValueError):
        await conn.list_events(CAL, naive, START)
    with pytest.raises(ValueError):
        await conn.list_events(CAL, START, START)
    with pytest.raises(ValueError, match="일정 ID"):
        await conn.create_event(CAL, "BAD", draft())
    assert rec.requests == []
