"""알림 API (ADR-0021 §6): 도착·예정 목록, 읽음, 취소, 설정, 격리 404, CSRF, 422. 합성 데이터."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from integration.mailindex_support import ACCOUNT, NOW, IndexEnv
from integration.notify_support import add_event, insert_notification, sync
from integration.runflow_support import StubResolver
from integration.support import WorkspaceFactory
from orbit.api.app import create_app
from orbit.auth import issue_session
from orbit.core.config import Settings

MIN = timedelta(minutes=1)


@dataclass
class Api:
    ix: IndexEnv
    client: httpx.AsyncClient
    transport: httpx.ASGITransport
    extra: list[httpx.AsyncClient] = field(default_factory=list)

    def headers(self, client: httpx.AsyncClient | None = None) -> dict[str, str]:
        return {"X-CSRF-Token": (client or self.client).cookies.get("orbit_csrf") or ""}

    async def other_client(self, workspace: uuid.UUID) -> httpx.AsyncClient:
        issued = await issue_session(
            self.ix.db,
            workspace_id=workspace,
            clock=self.ix.clock,
            ids=self.ix.ids,
            ttl_seconds=3600,
        )
        client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        client.cookies.set("orbit_session", issued.token)
        client.cookies.set("orbit_csrf", issued.csrf_token)
        self.extra.append(client)
        return client

    async def arrived(self) -> dict[str, Any]:
        response = await self.client.get("/api/notifications")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def upcoming(self) -> list[dict[str, Any]]:
        response = await self.client.get("/api/notifications/upcoming")
        assert response.status_code == 200, response.text
        found: list[dict[str, Any]] = response.json()["notifications"]
        return found


@pytest.fixture
async def api(app_url: str, ix: IndexEnv) -> AsyncIterator[Api]:
    settings = Settings(database_url=SecretStr(app_url), env="dev", dev_workspace_id=ix.ws)
    app = create_app(
        settings,
        clock=ix.clock,
        ids=ix.ids,
        resolver=StubResolver(ix.conns.calendar[ACCOUNT], ix.mail),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/dev/login")).status_code == 200
            made = Api(ix, client, transport)
            try:
                yield made
            finally:
                for extra in made.extra:
                    await extra.aclose()


async def test_arrived_list_shape_order_and_unread_count(api: Api) -> None:
    await insert_notification(api.ix, "합성 오래된", NOW - 10 * MIN)
    await insert_notification(api.ix, "합성 최근", NOW - 1 * MIN)
    await insert_notification(api.ix, "합성 예정", NOW + 5 * MIN)
    body = await api.arrived()
    assert [n["title"] for n in body["notifications"]] == ["합성 최근", "합성 오래된"]
    assert body["unread_count"] == 2
    first = body["notifications"][0]
    assert set(first) == {"id", "kind", "title", "due_at", "event_start", "read", "created_at"}
    assert first["kind"] == "reminder" and first["read"] is False and first["event_start"] is None
    assert [n["title"] for n in await api.upcoming()] == ["합성 예정"]


async def test_event_reminder_items_carry_event_start(api: Api) -> None:
    event = await add_event(api.ix, "합성 회의", NOW + 30 * MIN)
    await sync(api.ix)
    [item] = await api.upcoming()
    assert item["kind"] == "event_reminder" and item["title"] == "합성 회의"
    assert item["event_start"] == event.start.isoformat()


async def test_read_one_and_read_all(api: Api) -> None:
    first = await insert_notification(api.ix, "합성 A", NOW - 2 * MIN)
    await insert_notification(api.ix, "합성 B", NOW - 1 * MIN)
    future = await insert_notification(api.ix, "합성 C", NOW + 5 * MIN)
    h = api.headers()
    assert (await api.client.post(f"/api/notifications/{first}/read", headers=h)).status_code == 200
    assert (await api.client.post(f"/api/notifications/{first}/read", headers=h)).status_code == 200
    body = await api.arrived()
    assert body["unread_count"] == 1
    assert {n["title"]: n["read"] for n in body["notifications"]} == {
        "합성 A": True,
        "합성 B": False,
    }
    assert (
        await api.client.post(f"/api/notifications/{future}/read", headers=h)
    ).status_code == 404  # 도착 전
    done = await api.client.post("/api/notifications/read-all", headers=h)
    assert done.status_code == 200 and done.json() == {"updated": 1}
    assert (await api.arrived())["unread_count"] == 0


async def test_cancel_upcoming_only(api: Api) -> None:
    arrived = await insert_notification(api.ix, "합성 도착", NOW - MIN)
    future = await insert_notification(api.ix, "합성 예정", NOW + 5 * MIN)
    h = api.headers()
    assert (await api.client.delete(f"/api/notifications/{arrived}", headers=h)).status_code == 409
    assert (
        await api.client.delete(f"/api/notifications/{uuid.uuid4()}", headers=h)
    ).status_code == 404
    ok = await api.client.delete(f"/api/notifications/{future}", headers=h)
    assert ok.status_code == 200 and ok.json() == {"id": str(future)}
    assert await api.upcoming() == []
    assert (await api.client.delete(f"/api/notifications/{future}", headers=h)).status_code == 409
    api.ix.clock.advance(timedelta(hours=1))  # 취소한 알림은 도착하지 않는다
    assert [n["title"] for n in (await api.arrived())["notifications"]] == ["합성 도착"]


async def test_settings_default_save_and_validation(api: Api) -> None:
    got = await api.client.get("/api/notification-settings")
    assert got.status_code == 200
    assert got.json() == {"event_reminders_enabled": True, "lead_minutes": 10}
    h = api.headers()
    put = await api.client.put(
        "/api/notification-settings",
        json={"event_reminders_enabled": False, "lead_minutes": 30},
        headers=h,
    )
    assert put.status_code == 200 and put.json() == {
        "event_reminders_enabled": False,
        "lead_minutes": 30,
    }
    assert (await api.client.get("/api/notification-settings")).json()["lead_minutes"] == 30
    for bad in (
        {"event_reminders_enabled": True, "lead_minutes": 7},
        {"event_reminders_enabled": True},
        {"event_reminders_enabled": "yes", "lead_minutes": 10},
        {"event_reminders_enabled": True, "lead_minutes": 10, "extra": 1},
    ):
        resp = await api.client.put("/api/notification-settings", json=bad, headers=h)
        assert resp.status_code == 422, bad


async def test_setting_change_recomputes_pending_event_reminders(api: Api) -> None:
    await add_event(api.ix, "합성 회의", NOW + 90 * MIN)
    await sync(api.ix)
    [before] = await api.upcoming()
    resp = await api.client.put(
        "/api/notification-settings",
        json={"event_reminders_enabled": True, "lead_minutes": 60},
        headers=api.headers(),
    )
    assert resp.status_code == 200
    [after] = await api.upcoming()
    assert before["due_at"] != after["due_at"]
    assert after["due_at"] == (NOW + 30 * MIN).isoformat()


async def test_other_workspace_gets_404_and_sees_nothing(
    api: Api, workspaces: WorkspaceFactory
) -> None:
    mine = await insert_notification(api.ix, "합성 내 도착", NOW - MIN)
    mine_future = await insert_notification(api.ix, "합성 내 예정", NOW + 5 * MIN)
    other = await api.other_client(workspaces.create())
    h = api.headers(other)
    assert (await other.post(f"/api/notifications/{mine}/read", headers=h)).status_code == 404
    assert (await other.delete(f"/api/notifications/{mine_future}", headers=h)).status_code == 404
    assert (await other.get("/api/notifications")).json() == {
        "notifications": [],
        "unread_count": 0,
    }
    assert (await other.get("/api/notifications/upcoming")).json() == {"notifications": []}
    assert (await other.post("/api/notifications/read-all", headers=h)).json() == {"updated": 0}
    put = await other.put(
        "/api/notification-settings",
        json={"event_reminders_enabled": False, "lead_minutes": 5},
        headers=h,
    )
    assert put.status_code == 200
    assert (await api.client.get("/api/notification-settings")).json() == {
        "event_reminders_enabled": True,
        "lead_minutes": 10,
    }  # 다른 워크스페이스의 설정은 내 설정을 바꾸지 않는다
    assert (await api.arrived())["unread_count"] == 1  # 내 알림은 그대로다
    assert len(await api.upcoming()) == 1


async def test_state_changing_requests_need_csrf_and_requests_need_a_session(api: Api) -> None:
    nid = await insert_notification(api.ix, "합성", NOW - MIN)
    bad = {"X-CSRF-Token": "wrong"}
    for method, url, body in (
        ("POST", f"/api/notifications/{nid}/read", None),
        ("POST", "/api/notifications/read-all", None),
        ("DELETE", f"/api/notifications/{nid}", None),
        (
            "PUT",
            "/api/notification-settings",
            {"event_reminders_enabled": True, "lead_minutes": 10},
        ),
    ):
        missing = await api.client.request(method, url, json=body)
        wrong = await api.client.request(method, url, json=body, headers=bad)
        assert (missing.status_code, wrong.status_code) == (403, 403), (method, url)
    assert (await api.arrived())["unread_count"] == 1
    async with httpx.AsyncClient(transport=api.transport, base_url="http://test") as anon:
        for url in (
            "/api/notifications",
            "/api/notifications/upcoming",
            "/api/notification-settings",
        ):
            assert (await anon.get(url)).status_code == 401


async def test_invalid_ids_are_422(api: Api) -> None:
    h = api.headers()
    assert (
        await api.client.post("/api/notifications/not-a-uuid/read", headers=h)
    ).status_code == 422
    assert (await api.client.delete("/api/notifications/not-a-uuid", headers=h)).status_code == 422
