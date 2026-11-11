"""쓰기 Run 통합 테스트 공통 도구. 데이터는 모두 합성 값이다.

실제 PostgreSQL(`orbit_test`), 실제 LangGraph 체크포인터, 실제 Procrastinate 작업 큐를 쓰고
LLM(`ScriptedLLM`)과 커넥터(Sim)만 대체한다. 워커는 Procrastinate in-process 실행(`wait=False`)으로
큐가 빌 때까지 처리하므로 결정적이다. 시계는 `FixedClock`이다.
"""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import procrastinate
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url

from integration.support import WorkspaceFactory
from orbit.api.app import create_app
from orbit.auth import issue_session
from orbit.connectors.contracts import (
    Calendar,
    CalendarConnector,
    CalendarEvent,
    EventDraft,
    EventPatch,
    MailConnector,
)
from orbit.connectors.sim.factory import SimConnectorSet, build_sim_connectors
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.world import SimWorld, WorldDiff, diff
from orbit.core.clock import FixedClock
from orbit.core.config import Settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.evalrunner.llm_script import build_response
from orbit.llm.scripted import ScriptedLLM
from orbit.records.runs import RunEventRecord, list_events
from orbit.runflow.deps import RunDeps
from orbit.runflow.worker import build_worker_app

WORLD_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "sim" / "minimal_world.json"
ACCOUNT = "acct-alice"
NOW = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)  # 2026-11-11 09:00 KST
TZ = ZoneInfo("Asia/Seoul")

USER_MARKER = "SYNTH-USER-MARK-7f3a"  # 사용자 발화에만 넣는 합성 표식
BODY_MARKER = "SYNTH-BODY-MARK-91c2"  # 메일 본문에만 넣는 합성 표식
EXTERNAL_ONLY_EMAIL = "ext-only@example.net"  # 본문에서만 나오는 주소
DAY = {"time_min": "2026-11-12T00:00:00+09:00", "time_max": "2026-11-13T00:00:00+09:00"}


def world_data() -> dict[str, Any]:
    """minimal_world에서 첫 메일 본문에 합성 표식과 본문 전용 주소를 넣은 스냅샷."""
    data: dict[str, Any] = json.loads(WORLD_PATH.read_text(encoding="utf-8"))
    for message in data["accounts"][ACCOUNT]["messages"]:
        if message["id"] == "msg-a-001":
            message["body_text"] = f"{BODY_MARKER} 담당자는 {EXTERNAL_ONLY_EMAIL} 입니다."
    return data


class FlakyCalendar:
    """Sim 캘린더 래퍼. 장애 상황(쓰기 직후 처리기 중단, 조회 한 번 실패)을 만든다."""

    def __init__(self, inner: CalendarConnector) -> None:
        self._inner = inner
        self.crash_after_create = False
        self.fail_list_once = False

    async def list_calendars(self) -> list[Calendar]:
        return await self._inner.list_calendars()

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        query: str | None = None,
        include_cancelled: bool = False,
    ) -> list[CalendarEvent]:
        if self.fail_list_once:
            self.fail_list_once = False
            raise RuntimeError("조회 중 처리기 중단(테스트)")
        return await self._inner.list_events(
            calendar_id, time_min, time_max, query=query, include_cancelled=include_cancelled
        )

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        return await self._inner.get_event(calendar_id, event_id)

    async def create_event(
        self, calendar_id: str, event_id: str, draft: EventDraft
    ) -> CalendarEvent:
        event = await self._inner.create_event(calendar_id, event_id, draft)
        if self.crash_after_create:
            self.crash_after_create = False
            raise RuntimeError("외부 쓰기 직후 처리기 중단(테스트)")
        return event

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch, *, if_match: str
    ) -> CalendarEvent:
        return await self._inner.update_event(calendar_id, event_id, patch, if_match=if_match)

    async def cancel_event(
        self, calendar_id: str, event_id: str, *, if_match: str
    ) -> CalendarEvent:
        return await self._inner.cancel_event(calendar_id, event_id, if_match=if_match)


class StubResolver:
    def __init__(self, calendar: CalendarConnector, mail: MailConnector) -> None:
        self._pair = (calendar, mail)

    async def connectors_for(
        self, workspace_id: uuid.UUID
    ) -> tuple[CalendarConnector, MailConnector]:
        return self._pair


@dataclass
class Harness:
    db: AsyncDatabase
    ws: uuid.UUID
    clock: FixedClock
    llm: ScriptedLLM
    world: SimWorld
    before: SimWorld
    conns: SimConnectorSet
    calendar: FlakyCalendar
    deps: RunDeps
    worker_app: procrastinate.App
    client: httpx.AsyncClient
    app_url: str
    transport: httpx.ASGITransport
    api_clock: FixedClock
    api_ids: Uuid7Generator
    run_ids: list[uuid.UUID] = field(default_factory=list)
    extra_clients: list[httpx.AsyncClient] = field(default_factory=list)

    # ---------------------------------------------------------- 요청

    def headers(self, client: httpx.AsyncClient | None = None) -> dict[str, str]:
        """상태를 바꾸는 요청에 붙일 CSRF 헤더. 기본은 로그인한 `self.client`의 쿠키 값이다."""
        return {"X-CSRF-Token": (client or self.client).cookies.get("orbit_csrf") or ""}

    async def client_for(self, workspace: uuid.UUID) -> httpx.AsyncClient:
        """다른 워크스페이스에 묶인 세션 쿠키를 가진 클라이언트(세션은 서비스로 직접 발급한다)."""
        issued = await issue_session(
            self.db,
            workspace_id=workspace,
            clock=self.api_clock,
            ids=self.api_ids,
            ttl_seconds=3600,
        )
        client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        client.cookies.set("orbit_session", issued.token)
        client.cookies.set("orbit_csrf", issued.csrf_token)
        self.extra_clients.append(client)
        return client

    async def start(self, message: str) -> uuid.UUID:
        response = await self.client.post(
            "/api/runs", json={"message": message}, headers=self.headers()
        )
        assert response.status_code == 201, response.text
        run_id = uuid.UUID(response.json()["run_id"])
        self.run_ids.append(run_id)
        return run_id

    async def decide(
        self, approval_id: uuid.UUID, decision: str, client: httpx.AsyncClient | None = None
    ) -> httpx.Response:
        who = client or self.client
        return await who.post(
            f"/api/approvals/{approval_id}/decision",
            json={"decision": decision},
            headers=self.headers(who),
        )

    async def work(self) -> None:
        """큐에 도착한 작업을 모두 처리하고 돌아온다(in-process 워커)."""
        async with self.worker_app.open_async():
            await self.worker_app.run_worker_async(
                queues=["default"],
                wait=False,
                listen_notify=False,
                install_signal_handlers=False,
                concurrency=1,
            )

    # ---------------------------------------------------------- 조회

    async def run_row(self, run_id: uuid.UUID) -> dict[str, Any]:
        response = await self.client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def status(self, run_id: uuid.UUID) -> str:
        return str((await self.run_row(run_id))["status"])

    async def events(self, run_id: uuid.UUID) -> list[RunEventRecord]:
        async with self.db.workspace_session(self.ws) as s:
            return await list_events(s, workspace_id=self.ws, run_id=run_id)

    async def event_types(self, run_id: uuid.UUID) -> list[str]:
        return [e.type for e in await self.events(run_id)]

    async def approval_id(self, run_id: uuid.UUID) -> uuid.UUID:
        async with self.db.workspace_session(self.ws) as s:
            row = (
                await s.execute(
                    text("SELECT id FROM approval WHERE run_id = :r ORDER BY created_at DESC"),
                    {"r": run_id},
                )
            ).first()
        assert row is not None
        found: uuid.UUID = row[0]
        return found

    async def count(self, sql: str, **params: Any) -> int:
        async with self.db.workspace_session(self.ws) as s:
            return int((await s.execute(text(sql), params)).scalar_one())

    async def jobs(self, task_name: str | None = None) -> list[tuple[int, str, str, int]]:
        """이 워크스페이스의 작업 (id, task_name, status, attempts)."""
        async with self.db.workspace_session(self.ws) as s:
            rows = await s.execute(
                text(
                    "SELECT id, task_name, status::text, attempts FROM procrastinate_jobs"
                    " WHERE args->>'workspace_id' = :ws"
                    " AND (CAST(:task AS text) IS NULL OR task_name = :task) ORDER BY id"
                ),
                {"ws": str(self.ws), "task": task_name},
            )
            return [(r[0], r[1], r[2], r[3]) for r in rows]

    async def make_jobs_due(self) -> None:
        """재시도 대기 중인 작업을 지금 실행 대상으로 만든다(대기 시간이 지난 것과 같다)."""
        async with self.db.workspace_session(self.ws) as s:
            await s.execute(
                text(
                    "UPDATE procrastinate_jobs SET scheduled_at = now() - interval '1 second'"
                    " WHERE status = 'todo' AND args->>'workspace_id' = :ws"
                ),
                {"ws": str(self.ws)},
            )

    async def expire_leases(self) -> None:
        """강제 종료된 실행자의 소유권이 만료된 것과 같은 상태로 만든다."""
        async with self.db.workspace_session(self.ws) as s:
            await s.execute(
                text(
                    "UPDATE execution SET lease_expires_at = now() - interval '1 second'"
                    " WHERE lease_owner IS NOT NULL"
                )
            )

    def world_diff(self) -> WorldDiff:
        return diff(self.before, self.world)

    def write_calls(self) -> list[str]:
        """Sim이 받은 외부 쓰기 호출 이름(성공·실패 모두)."""
        writes = {"create_event", "update_event", "cancel_event", "create_draft", "send_draft"}
        return [r.tool for r in self.conns.calls.records if r.tool in writes]


HarnessFactory = Callable[..., Any]


async def build_harness(
    app_url: str,
    async_db: AsyncDatabase,
    workspaces: WorkspaceFactory,
    script: list[dict[str, Any]],
    *,
    plan: dict[str, Any] | None = None,
    approval_ttl: timedelta = timedelta(minutes=15),
    lease_seconds: float = 300.0,
) -> AsyncIterator[Harness]:
    ws = workspaces.create()
    async with async_db.workspace_session(ws) as s:  # 다른 테스트가 남긴 대기 작업을 치운다
        await s.execute(
            text("DELETE FROM procrastinate_jobs WHERE queue_name = 'default' AND status = 'todo'")
        )
    clock = FixedClock(NOW)
    ids = Uuid7Generator(clock)
    world = SimWorld.load(world_data(), clock)
    before = world.copy()
    conns = build_sim_connectors(world, [ACCOUNT], FaultPlan.from_dict(plan) if plan else None)
    calendar = FlakyCalendar(conns.calendar[ACCOUNT])
    llm = ScriptedLLM([build_response(item) for item in script])
    conninfo = make_url(app_url).set(drivername="postgresql").render_as_string(False)
    deps = RunDeps(
        db=async_db,
        clock=clock,
        ids=ids,
        llm=llm,
        resolver=StubResolver(calendar, conns.mail[ACCOUNT]),
        tz=TZ,
        checkpoint_conninfo=conninfo,
        approval_ttl=approval_ttl,
        lease_seconds=lease_seconds,
    )
    worker_app = build_worker_app(app_url, deps)
    settings = Settings(database_url=SecretStr(app_url), env="dev", dev_workspace_id=ws)
    api = create_app(settings, clock=clock, ids=ids)
    async with api.router.lifespan_context(api):
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post("/api/dev/login")
            assert login.status_code == 200, login.text
            harness = Harness(
                db=async_db,
                ws=ws,
                clock=clock,
                llm=llm,
                world=world,
                before=before,
                conns=conns,
                calendar=calendar,
                deps=deps,
                worker_app=worker_app,
                client=client,
                app_url=app_url,
                transport=transport,
                api_clock=clock,
                api_ids=ids,
            )
            try:
                yield harness
            finally:
                for extra in harness.extra_clients:
                    await extra.aclose()
                async with async_db.workspace_session(ws) as s:
                    await s.execute(
                        text("DELETE FROM procrastinate_jobs WHERE args->>'workspace_id' = :ws"),
                        {"ws": str(ws)},
                    )
                    for run_id in harness.run_ids:
                        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                            await s.execute(
                                text(f"DELETE FROM {table} WHERE thread_id = :t"),  # noqa: S608
                                {"t": str(run_id)},
                            )
