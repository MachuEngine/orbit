"""색인 범위 API (ADR-0019 §1·§6): 격리, CSRF, 범위 추가·삭제, 작업 등록. 데이터는 합성 값이다."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from integration.mailindex_support import IndexEnv
from integration.runflow_support import StubResolver
from integration.support import WorkspaceFactory
from orbit.api.app import create_app
from orbit.auth import issue_session
from orbit.connections.service import disconnect
from orbit.connectors.contracts import MailLabel, RateLimited
from orbit.core.config import Settings
from orbit.mailindex.sync import sync_connection


@dataclass
class Api:
    ix: IndexEnv
    client: httpx.AsyncClient
    transport: httpx.ASGITransport
    app: Any
    extra: list[httpx.AsyncClient] = field(default_factory=list)

    def headers(self, client: httpx.AsyncClient | None = None) -> dict[str, str]:
        return {"X-CSRF-Token": (client or self.client).cookies.get("orbit_csrf") or ""}

    @property
    def base(self) -> str:
        return f"/api/connections/{self.ix.cid}"

    async def post_scope(self, label_id: str = "INBOX", days: int = 365) -> httpx.Response:
        return await self.client.post(
            f"{self.base}/index/scopes",
            json={"label_id": label_id, "window_days": days},
            headers=self.headers(),
        )

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


@pytest.fixture
async def api(app_url: str, ix: IndexEnv) -> AsyncIterator[Api]:
    settings = Settings(database_url=SecretStr(app_url), env="dev", dev_workspace_id=ix.ws)
    app = create_app(
        settings,
        clock=ix.clock,
        ids=ix.ids,
        resolver=StubResolver(ix.conns.calendar["acct-alice"], ix.mail),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/dev/login")).status_code == 200
            made = Api(ix, client, transport, app)
            try:
                yield made
            finally:
                for extra in made.extra:
                    await extra.aclose()


async def job_queues(ix: IndexEnv) -> list[str]:
    async with ix.db.workspace_session(ix.ws) as s:
        rows = await s.execute(
            text(
                "SELECT queue_name FROM procrastinate_jobs WHERE task_name = 'sync_mail_index'"
                " AND args->>'workspace_id' = :ws"
            ),
            {"ws": str(ix.ws)},
        )
        return [r[0] for r in rows]


async def scope_jobs(ix: IndexEnv) -> list[dict[str, Any]]:
    async with ix.db.workspace_session(ix.ws) as s:
        rows = await s.execute(
            text(
                "SELECT args FROM procrastinate_jobs WHERE task_name = 'sync_mail_index'"
                " AND args->>'workspace_id' = :ws"
            ),
            {"ws": str(ix.ws)},
        )
        return [r[0] for r in rows]


async def test_labels_lists_connector_labels(api: Api) -> None:
    api.ix.state.user_labels["Label_1"] = "프로젝트"
    resp = await api.client.get(f"{api.base}/labels")
    assert resp.status_code == 200
    labels = {x["id"]: x for x in resp.json()["labels"]}
    assert labels["INBOX"] == {"id": "INBOX", "name": "INBOX", "kind": "system"}
    assert labels["Label_1"] == {"id": "Label_1", "name": "프로젝트", "kind": "user"}


async def test_labels_connector_error_maps_to_kind(api: Api) -> None:
    class Failing:
        async def connectors_for(self, workspace_id: uuid.UUID) -> Any:
            raise RateLimited(reason="rateLimitExceeded")

    api.app.state.resolver = Failing()
    resp = await api.client.get(f"{api.base}/labels")
    assert resp.status_code == 502 and resp.json() == {"detail": "rate_limited"}


async def test_other_workspace_unknown_and_revoked_connections_are_404(
    api: Api, workspaces: WorkspaceFactory
) -> None:
    other = await api.other_client(workspaces.create())

    async def check(client: httpx.AsyncClient, cid: uuid.UUID) -> None:
        base, h = f"/api/connections/{cid}", api.headers(client)
        sid = uuid.uuid4()
        results = [
            await client.get(f"{base}/labels"),
            await client.get(f"{base}/index"),
            await client.post(
                f"{base}/index/scopes", json={"label_id": "INBOX", "window_days": 30}, headers=h
            ),
            await client.delete(f"{base}/index/scopes/{sid}", headers=h),
            await client.post(f"{base}/index/sync", headers=h),
        ]
        assert [r.status_code for r in results] == [404] * 5

    await check(other, api.ix.cid)  # 다른 워크스페이스
    await check(api.client, uuid.uuid4())  # 없는 연결
    await disconnect(
        api.ix.db, None, None, workspace_id=api.ix.ws, connection_id=api.ix.cid, clock=api.ix.clock
    )
    await check(api.client, api.ix.cid)  # 해제된 연결


async def test_other_workspace_cannot_delete_or_see_scopes(
    api: Api, workspaces: WorkspaceFactory
) -> None:
    created = (await api.post_scope()).json()
    other = await api.other_client(workspaces.create())
    resp = await other.delete(
        f"{api.base}/index/scopes/{created['id']}", headers=api.headers(other)
    )
    assert resp.status_code == 404
    assert len((await api.client.get(f"{api.base}/index")).json()["scopes"]) == 1


async def test_state_changing_requests_need_csrf(api: Api) -> None:
    body = {"label_id": "INBOX", "window_days": 30}
    assert (await api.client.post(f"{api.base}/index/scopes", json=body)).status_code == 403
    assert (await api.client.post(f"{api.base}/index/sync")).status_code == 403
    bad = {"X-CSRF-Token": "wrong"}
    assert (
        await api.client.delete(f"{api.base}/index/scopes/{uuid.uuid4()}", headers=bad)
    ).status_code == 403
    assert (await api.client.get(f"{api.base}/index")).json()["scopes"] == []


async def test_unauthenticated_requests_are_401(api: Api) -> None:
    async with httpx.AsyncClient(transport=api.transport, base_url="http://test") as anon:
        assert (await anon.get(f"{api.base}/index")).status_code == 401


async def test_add_scope_stores_server_side_name_and_registers_one_id_only_job(api: Api) -> None:
    api.ix.state.user_labels["Label_1"] = "프로젝트"
    resp = await api.post_scope("Label_1", 90)
    assert resp.status_code == 201
    body = resp.json()
    assert (body["label_id"], body["label_name"], body["window_days"], body["synced"]) == (
        "Label_1",
        "프로젝트",
        90,
        False,
    )
    again = await api.post_scope("Label_1", 30)  # 같은 라벨은 기간만 바뀐다
    assert again.status_code == 201 and again.json()["id"] == body["id"]
    assert await scope_jobs(api.ix) == [
        {"workspace_id": str(api.ix.ws), "connection_id": str(api.ix.cid)}
    ]
    assert await job_queues(api.ix) == ["mail_index"]  # run 큐와 나뉜 색인 큐
    status = (await api.client.get(f"{api.base}/index")).json()
    assert status["status"] == "pending" and status["sync_queued"] is True
    assert [(x["label_id"], x["window_days"]) for x in status["scopes"]] == [("Label_1", 30)]


@pytest.mark.parametrize(
    "body",
    [
        {"label_id": "INBOX", "window_days": 7},
        {"label_id": "INBOX"},
        {"window_days": 30},
        {"label_id": "INBOX", "window_days": 30, "label_name": "해킹"},  # 이름은 믿지 않는다
        {"label_id": "INBOX", "window_days": "30"},
    ],
)
async def test_add_scope_validates_input(api: Api, body: dict[str, Any]) -> None:
    resp = await api.client.post(f"{api.base}/index/scopes", json=body, headers=api.headers())
    assert resp.status_code == 422
    assert (await api.client.get(f"{api.base}/index")).json()["scopes"] == []


async def test_unknown_label_is_422(api: Api) -> None:
    resp = await api.post_scope("Label_nope", 30)
    assert resp.status_code == 422 and resp.json() == {"detail": "unknown_label"}


async def test_status_after_sync_has_counts_and_no_mail_content(api: Api) -> None:
    await api.post_scope("INBOX", 365)
    await sync_connection(api.ix.mi, api.ix.mail, api.ix.ws, api.ix.cid)
    resp = await api.client.get(f"{api.base}/index")
    body = resp.json()
    assert body["status"] == "ok" and body["indexed_mail_count"] == 5
    assert body["last_synced_at"] == "2026-11-11T09:00:00+00:00"
    assert body["scopes"][0]["synced"] is True and body["error_kind"] is None
    text_body = resp.text
    for secret in ("견적", "msg-a-001", "bob@example.org", "subject"):
        assert secret not in text_body


async def test_delete_scope_cleans_up_index(api: Api) -> None:
    scope = (await api.post_scope("INBOX", 365)).json()
    await sync_connection(api.ix.mi, api.ix.mail, api.ix.ws, api.ix.cid)
    assert await api.ix.scalar("SELECT count(*) FROM chunk") > 0
    resp = await api.client.delete(f"{api.base}/index/scopes/{scope['id']}", headers=api.headers())
    assert resp.status_code == 200
    for table in ("source_item", "chunk", "index_scope", "sync_state"):
        assert await api.ix.scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    again = await api.client.delete(f"{api.base}/index/scopes/{scope['id']}", headers=api.headers())
    assert again.status_code == 404
    assert (await api.client.get(f"{api.base}/index")).json()["status"] == "none"


async def test_sync_now_is_202_and_does_not_double_register(api: Api) -> None:
    empty = await api.client.post(f"{api.base}/index/sync", headers=api.headers())
    assert empty.status_code == 202 and empty.json() == {"queued": False}  # 범위가 없다
    await api.post_scope()  # 작업 하나가 이미 대기 중이다
    again = await api.client.post(f"{api.base}/index/sync", headers=api.headers())
    assert again.status_code == 202 and again.json() == {"queued": False}
    assert len(await scope_jobs(api.ix)) == 1
    async with api.ix.db.workspace_session(api.ix.ws) as s:  # 대기 작업이 끝났다고 가정
        await s.execute(
            text("DELETE FROM procrastinate_jobs WHERE args->>'workspace_id' = :ws"),
            {"ws": str(api.ix.ws)},
        )
    fresh = await api.client.post(f"{api.base}/index/sync", headers=api.headers())
    assert fresh.status_code == 202 and fresh.json() == {"queued": True}
    assert len(await scope_jobs(api.ix)) == 1


class _LabelsOnly:
    """주어진 라벨만 돌려주는 메일 커넥터 자리."""

    def __init__(self, labels: list[MailLabel]) -> None:
        self._labels = labels

    async def list_labels(self) -> list[MailLabel]:
        return self._labels


@pytest.mark.parametrize(
    "label_id",
    ["UNREAD", "YELLOW_STAR", "RED_BANG", "ORANGE_GUILLEMET", "GREEN_CHECK", "DRAFT", "SPAM"],
)
async def test_state_flag_and_superstar_labels_are_rejected_with_422(
    api: Api, label_id: str
) -> None:
    labels = [
        MailLabel(id="INBOX", name="INBOX", kind="system"),
        MailLabel(id="STARRED", name="STARRED", kind="system"),
        MailLabel(id="IMPORTANT", name="IMPORTANT", kind="system"),
        MailLabel(id=label_id, name=label_id, kind="system"),
    ]
    api.app.state.resolver = StubResolver(None, _LabelsOnly(labels))  # type: ignore[arg-type]
    resp = await api.post_scope(label_id, 30)
    assert resp.status_code == 422 and resp.json() == {"detail": "unsupported_label"}
    assert (await api.client.get(f"{api.base}/index")).json()["scopes"] == []
    for kept in ("INBOX", "STARRED", "IMPORTANT"):  # 유지하는 라벨은 추가된다
        assert (await api.post_scope(kept, 30)).status_code == 201
