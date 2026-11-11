"""Google 모의 서버 전용 회귀 테스트. 어댑터가 PATCH의 배열 통째 교체 의미와 맞물리는지 본다."""

from datetime import UTC, datetime

import httpx

from orbit.connectors.contracts import Attendee, EventPatch
from orbit.connectors.google.calendar import GoogleCalendarConnector
from orbit.connectors.sim.world import SimWorld

from .google_calendar_mock import MOCK_BASE_URL, GoogleCalendarMock, mock_transport
from .register_google_calendar import StaticTokens

MAIN = "cal-alice-main"
EV1 = "evalice001"  # 시드 참석자: bob@example.org


async def test_attendee_patch_keeps_existing_response_status(minimal_world: SimWorld) -> None:
    mock = GoogleCalendarMock.from_account(
        minimal_world.account("acct-alice"), minimal_world.clock.now()
    )
    client = httpx.AsyncClient(base_url=MOCK_BASE_URL, transport=mock_transport(mock))
    conn = GoogleCalendarConnector(
        StaticTokens("mock-token"), client=client, allow_test_base_url=True
    )
    # 밥이 수락한 상태를 서버 쪽에서 만든다(계약 밖 동작이라 모의 서버 저장소를 직접 바꾼다).
    mock.events[MAIN][EV1]["attendees"][0]["responseStatus"] = "accepted"
    ev = await conn.get_event(MAIN, EV1)
    assert ev.attendees[0].response_status == "accepted"

    patch = EventPatch(
        attendees=(
            Attendee(email="bob@example.org", name="밥"),
            Attendee(email="carol@example.org"),
        )
    )
    updated = await conn.update_event(MAIN, EV1, patch, if_match=ev.version)
    by_email = {a.email: a.response_status for a in updated.attendees}
    assert by_email == {"bob@example.org": "accepted", "carol@example.org": "needs_action"}
    assert updated.start == datetime(2026, 11, 12, 1, tzinfo=UTC)


async def test_mock_replaces_attendees_and_drops_status_when_not_sent(
    minimal_world: SimWorld,
) -> None:
    """모의 서버 자체 검증: 상태를 싣지 않고 보내면 서버는 needsAction으로 되돌린다."""
    mock = GoogleCalendarMock.from_account(
        minimal_world.account("acct-alice"), minimal_world.clock.now()
    )
    mock.events[MAIN][EV1]["attendees"][0]["responseStatus"] = "accepted"
    client = httpx.AsyncClient(base_url=MOCK_BASE_URL, transport=mock_transport(mock))
    etag = mock.events[MAIN][EV1]["etag"]
    resp = await client.patch(
        f"/calendars/{MAIN}/events/{EV1}",
        json={"attendees": [{"email": "bob@example.org"}]},
        headers={"Authorization": "Bearer t", "If-Match": etag},
    )
    assert resp.json()["attendees"] == [
        {"email": "bob@example.org", "responseStatus": "needsAction"}
    ]
