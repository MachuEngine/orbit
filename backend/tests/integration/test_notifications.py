"""예약 알림: 일정 알림 동기화, 설정, 도착 판정, 선별 함수, RLS, 삭제 전파, 작업 큐 (ADR-0021).

데이터는 모두 합성 값이고 시계는 `FixedClock`이다.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from integration.mailindex_support import ACCOUNT, NOW, IndexEnv, build_env, purge_jobs
from integration.notify_support import (
    MY_EMAIL,
    add_event,
    insert_notification,
    move_event,
    rows,
    sync,
)
from integration.runflow_support import StubResolver
from integration.support import WorkspaceFactory
from orbit.connections.service import disconnect
from orbit.connectors.contracts import Attendee, PermissionRevoked, TransientError
from orbit.db.session import AsyncDatabase
from orbit.notify import service
from orbit.notify.jobs import (
    NOTIFY_QUEUE,
    SYNC_EVENT_REMINDERS,
    defer_event_sync,
    enqueue_event_syncs,
    validate_sync_args,
)
from orbit.records.errors import InvalidJobArgs
from orbit.runflow.deps import RunDeps
from orbit.runflow.worker import WorkerPool, build_worker_app

MIN = timedelta(minutes=1)

# ---------------------------------------------------------------- 일정 알림 동기화


async def test_creates_reminders_only_for_events_starting_within_two_hours(ix: IndexEnv) -> None:
    soon = await add_event(ix, "합성 주간 회의", NOW + 30 * MIN)
    await add_event(ix, "합성 늦은 회의", NOW + timedelta(hours=3))  # 창 밖
    result = await sync(ix)
    assert (result.outcome, result.created) == ("synced", 1)
    [row] = await rows(ix)
    assert (row["kind"], row["title"], row["status"]) == (
        "event_reminder",
        "합성 주간 회의",
        "scheduled",
    )
    assert row["event_id"] == soon.id and row["event_start"] == soon.start
    assert row["due_at"] == soon.start - 10 * MIN  # 기본 10분 전
    assert row["read_at"] is None and row["run_id"] is None
    again = await sync(ix)  # 두 번째 동기화는 같은 알림을 고친다(중복 없음)
    assert (again.created, again.updated) == (0, 1) and len(await rows(ix)) == 1


async def test_start_time_change_moves_due_at_and_moving_out_of_window_removes_it(
    ix: IndexEnv,
) -> None:
    event = await add_event(ix, "합성 회의", NOW + 30 * MIN)
    await sync(ix)
    moved = await move_event(ix, event, NOW + 50 * MIN)
    await sync(ix)
    [row] = await rows(ix)
    assert row["event_start"] == moved.start and row["due_at"] == moved.start - 10 * MIN

    later = await move_event(ix, moved, NOW + timedelta(hours=5))  # 조회 창 밖
    result = await sync(ix)
    assert result.removed == 1 and await rows(ix) == []
    await move_event(ix, later, NOW + 40 * MIN)  # 다시 창 안으로 들어오면 새로 만든다
    assert (await sync(ix)).created == 1


async def test_cancelled_event_cancels_pending_reminder_and_is_not_revived(ix: IndexEnv) -> None:
    event = await add_event(ix, "합성 취소될 회의", NOW + 30 * MIN)
    await sync(ix)
    await ix.conns.calendar[ACCOUNT].cancel_event(
        event.calendar_id, event.id, if_match=event.version
    )
    result = await sync(ix)
    assert result.cancelled == 1
    [row] = await rows(ix)
    assert row["status"] == "cancelled"
    assert await service.list_upcoming(ix.db, ix.clock, ix.ws) == []
    await sync(ix)  # 다시 동기화해도 되살리지 않는다
    assert [r["status"] for r in await rows(ix)] == ["cancelled"]


async def test_declined_by_me_all_day_and_started_events_are_not_reminded(ix: IndexEnv) -> None:
    ix.clock.set(NOW.replace(hour=23))  # 종일 일정(UTC 00:00 시작)이 두 시간 창에 들어온다
    now = ix.clock.now()
    await add_event(
        ix,
        "합성 거절한 회의",
        now + 30 * MIN,
        attendees=(Attendee(email=MY_EMAIL.upper(), response_status="declined"),),
    )
    await add_event(
        ix,
        "합성 남이 거절한 회의",
        now + 40 * MIN,
        attendees=(Attendee(email="bob@example.org", response_status="declined"),),
    )
    await add_event(ix, "합성 종일 일정", NOW.replace(hour=0) + timedelta(days=1), all_day=True)
    await add_event(ix, "합성 이미 시작한 회의", now - 5 * MIN, minutes=60)
    result = await sync(ix)
    assert result.created == 1
    assert [r["title"] for r in await rows(ix)] == ["합성 남이 거절한 회의"]


async def test_event_declined_after_reminder_was_created_cancels_it(ix: IndexEnv) -> None:
    event = await add_event(ix, "합성 회의", NOW + 30 * MIN)
    await sync(ix)
    patch_calendar = ix.conns.calendar[ACCOUNT]
    from orbit.connectors.contracts import EventPatch

    await patch_calendar.update_event(
        event.calendar_id,
        event.id,
        EventPatch(attendees=(Attendee(email=MY_EMAIL, response_status="declined"),)),
        if_match=event.version,
    )
    assert (await sync(ix)).cancelled == 1
    assert [r["status"] for r in await rows(ix)] == ["cancelled"]


async def test_event_about_to_start_arrives_immediately(ix: IndexEnv) -> None:
    await add_event(ix, "합성 곧 시작", NOW + 5 * MIN)  # 10분 전 시점이 이미 지났다
    await sync(ix)
    [row] = await rows(ix)
    assert row["due_at"] == NOW  # 과거가 아니라 지금으로 맞춘다
    arrived = await service.list_arrived(ix.db, ix.clock, ix.ws)
    assert [n.title for n in arrived.notifications] == ["합성 곧 시작"]


async def test_empty_title_gets_placeholder_and_long_title_is_cut(ix: IndexEnv) -> None:
    await add_event(ix, "가" * 500, NOW + 20 * MIN)
    await add_event(ix, "   ", NOW + 25 * MIN)
    await sync(ix)
    titles = sorted(r["title"] for r in await rows(ix))
    assert titles == sorted(["가" * 200, "(제목 없음)"])


async def test_setting_off_skips_sync_and_removes_pending_but_keeps_arrived(ix: IndexEnv) -> None:
    await add_event(ix, "합성 곧 시작", NOW + 5 * MIN)  # 바로 도착
    await add_event(ix, "합성 나중 회의", NOW + 60 * MIN)
    await sync(ix)
    assert len(await rows(ix)) == 2
    await service.save_settings(ix.db, ix.clock, ix.ws, enabled=False, lead_minutes=10)
    assert [r["title"] for r in await rows(ix)] == ["합성 곧 시작"]  # 도착 전 알림만 지웠다
    assert (await sync(ix)).outcome == "disabled"
    assert len(await rows(ix)) == 1
    await service.save_settings(ix.db, ix.clock, ix.ws, enabled=True, lead_minutes=10)
    assert (await sync(ix)).created == 1  # 다시 켜면 도착 전 알림을 새로 만든다


async def test_lead_change_recomputes_pending_due_at(ix: IndexEnv) -> None:
    await add_event(ix, "합성 회의 A", NOW + 90 * MIN)
    await add_event(ix, "합성 회의 B", NOW + 20 * MIN)
    await sync(ix)
    await service.save_settings(ix.db, ix.clock, ix.ws, enabled=True, lead_minutes=30)
    by_title = {r["title"]: r for r in await rows(ix)}
    assert by_title["합성 회의 A"]["due_at"] == NOW + 60 * MIN
    assert by_title["합성 회의 B"]["due_at"] == NOW  # 30분 전이 이미 지났다: 바로 도착
    await service.save_settings(ix.db, ix.clock, ix.ws, enabled=True, lead_minutes=5)
    by_title = {r["title"]: r for r in await rows(ix)}
    assert by_title["합성 회의 A"]["due_at"] == NOW + 85 * MIN
    assert by_title["합성 회의 B"]["due_at"] == NOW  # 도착한 알림은 다시 미루지 않는다
    with pytest.raises(service.InvalidSetting):
        await service.save_settings(ix.db, ix.clock, ix.ws, enabled=True, lead_minutes=7)


async def test_new_event_sync_uses_the_saved_lead(ix: IndexEnv) -> None:
    await service.save_settings(ix.db, ix.clock, ix.ws, enabled=True, lead_minutes=15)
    await add_event(ix, "합성 회의", NOW + 60 * MIN)
    await sync(ix)
    [row] = await rows(ix)
    assert row["due_at"] == NOW + 45 * MIN


async def test_user_cancelled_reminder_is_not_recreated_by_sync(ix: IndexEnv) -> None:
    await add_event(ix, "합성 회의", NOW + 60 * MIN)
    await sync(ix)
    [row] = await rows(ix)
    assert await service.cancel(ix.db, ix.clock, ix.ws, row["id"]) == "cancelled"
    result = await sync(ix)
    assert result.created == 0 and [r["status"] for r in await rows(ix)] == ["cancelled"]


class _Failing:
    """첫 `list_events`에서 오류를 던지는 캘린더(켜져 있는 동안)."""

    def __init__(self, inner: Any, error: Exception) -> None:
        self._inner = inner
        self.error: Exception | None = error

    async def list_events(self, *args: Any, **kwargs: Any) -> Any:
        if self.error is not None:
            raise self.error
        return await self._inner.list_events(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_connector_errors_log_kind_only_and_leave_no_backoff(
    ix: IndexEnv, caplog: pytest.LogCaptureFixture
) -> None:
    await add_event(ix, "SYNTH-TITLE-MARK-31c8", NOW + 60 * MIN)
    await sync(ix)
    before = await rows(ix)
    flaky = _Failing(ix.conns.calendar[ACCOUNT], TransientError("합성", reason="backend_error"))
    caplog.set_level("INFO")
    result = await sync(ix, flaky)
    assert (result.outcome, result.error_kind) == ("error", "transient_error")
    flaky.error = PermissionRevoked("합성", reason="invalid_grant")
    revoked = await sync(ix, flaky)
    assert (revoked.outcome, revoked.error_kind) == ("error", "permission_revoked")
    assert await rows(ix) == before  # 오류는 기존 알림을 건드리지 않는다
    assert "SYNTH-TITLE-MARK" not in caplog.text and "transient_error" in caplog.text
    # 백오프가 없다: 다음 주기 선별에도 그대로 들어가고, 오류가 풀리면 바로 동기화된다.
    assert (ix.ws, ix.cid) in await ix.db.unscoped_event_reminder_due_connections()
    flaky.error = None
    assert (await sync(ix, flaky)).outcome == "synced"


async def test_sync_is_quiet_for_missing_or_revoked_connection(ix: IndexEnv) -> None:
    await add_event(ix, "합성 회의", NOW + 30 * MIN)
    assert (await sync(ix)).outcome == "synced"
    await disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
    assert (await sync(ix)).outcome == "no_connection"
    assert await rows(ix, "kind = 'event_reminder'") == []  # 해제가 지웠고 동기화가 되살리지 않는다


async def test_logs_hold_counts_and_ids_but_never_titles(
    ix: IndexEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    await add_event(ix, "SYNTH-TITLE-MARK-77aa", NOW + 30 * MIN)
    await sync(ix)
    assert "created=1" in caplog.text and "SYNTH-TITLE-MARK" not in caplog.text


# ---------------------------------------------------------------- 도착 판정·읽음·취소


async def test_arrival_is_decided_by_due_at_against_the_fixed_clock(ix: IndexEnv) -> None:
    due_now = await insert_notification(ix, "합성 지금", NOW)
    await insert_notification(ix, "합성 과거", NOW - 5 * MIN)
    future = await insert_notification(ix, "합성 미래", NOW + 1 * MIN)
    arrived = await service.list_arrived(ix.db, ix.clock, ix.ws)
    assert [n.title for n in arrived.notifications] == ["합성 지금", "합성 과거"]  # 최근순
    assert arrived.unread_count == 2
    assert [n.title for n in await service.list_upcoming(ix.db, ix.clock, ix.ws)] == ["합성 미래"]
    assert not await service.mark_read(ix.db, ix.clock, ix.ws, future)  # 도착 전에는 읽을 수 없다
    ix.clock.advance(MIN)  # 시계만 움직이면 예정 알림이 도착한다(발송 작업이 없다)
    arrived = await service.list_arrived(ix.db, ix.clock, ix.ws)
    assert [n.title for n in arrived.notifications][0] == "합성 미래"
    assert await service.list_upcoming(ix.db, ix.clock, ix.ws) == []
    assert await service.mark_read(ix.db, ix.clock, ix.ws, due_now)
    assert (await service.list_arrived(ix.db, ix.clock, ix.ws)).unread_count == 2
    assert await service.mark_all_read(ix.db, ix.clock, ix.ws) == 2
    assert (await service.list_arrived(ix.db, ix.clock, ix.ws)).unread_count == 0
    assert await service.mark_all_read(ix.db, ix.clock, ix.ws) == 0  # 멱등


async def test_arrived_list_is_capped_at_fifty_and_unread_counts_all(ix: IndexEnv) -> None:
    for i in range(53):
        await insert_notification(ix, f"합성 알림 {i:02d}", NOW - i * MIN)
    arrived = await service.list_arrived(ix.db, ix.clock, ix.ws)
    assert len(arrived.notifications) == 50 and arrived.unread_count == 53
    assert arrived.notifications[0].title == "합성 알림 00"


async def test_only_upcoming_can_be_cancelled_and_cancelled_never_arrive(ix: IndexEnv) -> None:
    arrived = await insert_notification(ix, "합성 도착", NOW - MIN)
    future = await insert_notification(ix, "합성 예정", NOW + 10 * MIN)
    assert await service.cancel(ix.db, ix.clock, ix.ws, arrived) == "not_cancellable"
    assert await service.cancel(ix.db, ix.clock, ix.ws, uuid.uuid4()) == "not_found"
    assert await service.cancel(ix.db, ix.clock, ix.ws, future) == "cancelled"
    assert await service.cancel(ix.db, ix.clock, ix.ws, future) == "not_cancellable"
    ix.clock.advance(timedelta(hours=1))
    titles = [n.title for n in (await service.list_arrived(ix.db, ix.clock, ix.ws)).notifications]
    assert titles == ["합성 도착"]  # 취소한 알림은 시각이 지나도 도착하지 않는다
    assert not await service.mark_read(ix.db, ix.clock, ix.ws, future)


# ---------------------------------------------------------------- 선별 함수


async def test_due_selection_returns_active_connections_with_reminders_on(
    ix: IndexEnv, async_db: AsyncDatabase, workspaces: WorkspaceFactory, owner_engine: Engine
) -> None:
    other = await build_env(async_db, workspaces, owner_engine)
    due = await ix.db.unscoped_event_reminder_due_connections()
    assert (ix.ws, ix.cid) in due and (other.ws, other.cid) in due  # 설정 행이 없으면 켠 것이다
    await service.save_settings(ix.db, ix.clock, ix.ws, enabled=False, lead_minutes=10)
    due = await ix.db.unscoped_event_reminder_due_connections()
    assert (ix.ws, ix.cid) not in due and (other.ws, other.cid) in due
    await service.save_settings(ix.db, ix.clock, ix.ws, enabled=True, lead_minutes=30)
    assert (ix.ws, ix.cid) in await ix.db.unscoped_event_reminder_due_connections()
    await disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
    assert (ix.ws, ix.cid) not in await ix.db.unscoped_event_reminder_due_connections()


async def test_due_function_returns_ids_only_and_tables_stay_hidden(
    ix: IndexEnv, raw_engine: Engine
) -> None:
    with raw_engine.connect() as conn:
        cols = conn.execute(text("SELECT * FROM event_reminder_due_connections()"))
        assert list(cols.keys()) == ["workspace_id", "connection_id"]
        conn.rollback()
        # 테이블을 직접 읽으면 워크스페이스 설정이 없을 때 0행이다.
        for table in ("notification", "notification_setting"):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0  # noqa: S608
            conn.rollback()


# ---------------------------------------------------------------- RLS


async def test_other_workspaces_notifications_and_settings_are_invisible(
    ix: IndexEnv, async_db: AsyncDatabase, workspaces: WorkspaceFactory, owner_engine: Engine
) -> None:
    other = await build_env(async_db, workspaces, owner_engine)
    mine = await insert_notification(ix, "합성 내 알림", NOW - MIN)
    theirs = await insert_notification(other, "합성 남의 알림", NOW - MIN)
    await service.save_settings(other.db, other.clock, other.ws, enabled=False, lead_minutes=60)

    assert [
        n.title for n in (await service.list_arrived(ix.db, ix.clock, ix.ws)).notifications
    ] == ["합성 내 알림"]
    assert not await service.mark_read(ix.db, ix.clock, ix.ws, theirs)
    assert await service.cancel(ix.db, ix.clock, ix.ws, theirs) == "not_found"
    assert (await service.get_settings(ix.db, ix.clock, ix.ws)).event_reminders_enabled is True
    assert await service.mark_all_read(ix.db, ix.clock, ix.ws) == 1
    assert [r["read_at"] is None for r in await rows(other)] == [True]  # 남의 행은 그대로다
    assert mine != theirs


async def test_rows_for_another_workspace_cannot_be_written_or_seen_without_setting(
    ix: IndexEnv, workspaces: WorkspaceFactory, raw_engine: Engine
) -> None:
    other_ws = workspaces.create()
    with pytest.raises(DBAPIError):  # 다른 워크스페이스 값은 WITH CHECK가 거절한다
        await _insert_as(ix, other_ws)
    await insert_notification(ix, "합성 내 알림", NOW)  # 정상 경로는 쓸 수 있다
    with raw_engine.connect() as conn:  # 워크스페이스 설정이 없으면 아무것도 보이지 않는다
        assert conn.execute(text("SELECT count(*) FROM notification")).scalar_one() == 0
        conn.rollback()


async def _insert_as(ix: IndexEnv, other_ws: uuid.UUID) -> None:
    async with ix.db.workspace_session(ix.ws) as s:  # 내 워크스페이스 세션에서 남의 ID로 쓴다
        await s.execute(
            text(
                "INSERT INTO notification (id, workspace_id, kind, title, due_at, status,"
                " created_at) VALUES (:id, :ws, 'reminder', 'x', :now, 'scheduled', :now)"
            ),
            {"id": ix.ids.next(), "ws": other_ws, "now": NOW},
        )


# ---------------------------------------------------------------- 삭제 전파


async def test_disconnect_deletes_event_reminders_and_keeps_conversation_reminders(
    ix: IndexEnv,
) -> None:
    await add_event(ix, "합성 회의", NOW + 30 * MIN)
    await sync(ix)
    kept = await insert_notification(ix, "합성 대화 리마인더", NOW + 60 * MIN)
    assert len(await rows(ix, "kind = 'event_reminder'")) == 1
    await disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
    assert await rows(ix, "kind = 'event_reminder'") == []  # 0행
    assert [r["id"] for r in await rows(ix)] == [kept]


async def test_replacing_a_dead_connection_deletes_its_event_reminders(ix: IndexEnv) -> None:
    from pydantic import SecretStr

    from orbit.connections import store as connection_store
    from orbit.connections.oauth import DATA_SCOPES, ConnectGrant
    from orbit.connections.service import save_grant
    from orbit.crypto.envelope import EnvelopeCipher, KeyEncryptionKey

    await add_event(ix, "합성 회의", NOW + 30 * MIN)
    await sync(ix)
    async with ix.db.workspace_session(ix.ws) as s:
        user_id = (
            await s.execute(text("SELECT user_id FROM connection WHERE id = :id"), {"id": ix.cid})
        ).scalar_one()
        await connection_store.mark_dead(s, ix.ws, ix.cid, status="error", now=ix.clock.now())
    assert len(await rows(ix, "kind = 'event_reminder'")) == 1
    grant = ConnectGrant(
        sub="sub-new",
        email=MY_EMAIL,
        scopes=DATA_SCOPES,
        refresh_token=SecretStr("synthetic-refresh-token"),
    )
    await save_grant(
        ix.db,
        None,  # type: ignore[arg-type]
        EnvelopeCipher(KeyEncryptionKey(bytes(range(32)))),
        workspace_id=ix.ws,
        user_id=user_id,
        grant=grant,
        clock=ix.clock,
        ids=ix.ids,
    )
    assert await rows(ix, "kind = 'event_reminder'") == []


async def test_deleting_the_run_keeps_the_reminder_and_clears_run_id(
    ix: IndexEnv, owner_engine: Engine
) -> None:
    run_id = uuid.uuid4()
    nid = ix.ids.next()
    with owner_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ix.ws)})
        conv = ix.ids.next()
        conn.execute(
            text("INSERT INTO conversation (id, workspace_id, created_at) VALUES (:c, :w, :n)"),
            {"c": conv, "w": ix.ws, "n": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO run (id, workspace_id, conversation_id, status, pattern,"
                " config_fingerprint, created_at, updated_at)"
                " VALUES (:r, :w, :c, 'succeeded', 'write-v0', 'fp', :n, :n)"
            ),
            {"r": run_id, "w": ix.ws, "c": conv, "n": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO notification (id, workspace_id, kind, title, due_at, status,"
                " created_at, run_id) VALUES (:id, :w, 'reminder', '합성', :n, 'scheduled', :n, :r)"
            ),
            {"id": nid, "w": ix.ws, "n": NOW, "r": run_id},
        )
        conn.execute(text("DELETE FROM run WHERE id = :r"), {"r": run_id})
    [row] = await rows(ix)
    assert row["id"] == nid and row["run_id"] is None


# ---------------------------------------------------------------- 작업 인자·큐


async def test_job_args_are_ids_only(ix: IndexEnv) -> None:
    ok = validate_sync_args({"workspace_id": str(ix.ws), "connection_id": str(ix.cid)})
    assert set(ok) == {"workspace_id", "connection_id"}
    for bad in (
        {"workspace_id": str(ix.ws)},
        {"workspace_id": str(ix.ws), "connection_id": str(ix.cid), "title": "합성 제목"},
        {"workspace_id": "x", "connection_id": str(ix.cid)},
    ):
        with pytest.raises(InvalidJobArgs):
            validate_sync_args(bad)


def _deps(ix: IndexEnv, calendar: Any = None) -> RunDeps:
    return RunDeps(
        db=ix.db,
        clock=ix.clock,
        ids=ix.ids,
        llm=None,  # type: ignore[arg-type]
        resolver=StubResolver(calendar or ix.conns.calendar[ACCOUNT], ix.mail),
        tz=None,  # type: ignore[arg-type]
        checkpoint_conninfo="",
    )


async def _jobs(ix: IndexEnv) -> list[tuple[str, str, dict[str, Any]]]:
    async with ix.db.workspace_session(ix.ws) as s:
        found = await s.execute(
            text(
                "SELECT task_name, queue_name, args FROM procrastinate_jobs"
                " WHERE args->>'workspace_id' = :ws AND task_name = :t ORDER BY id"
            ),
            {"ws": str(ix.ws), "t": SYNC_EVENT_REMINDERS},
        )
        return [(r[0], r[1], r[2]) for r in found]


async def test_periodic_selection_registers_one_id_only_job_on_the_notify_queue(
    ix: IndexEnv, app_url: str
) -> None:
    app = build_worker_app(app_url, _deps(ix))
    await purge_jobs(ix.db, ix.ws)
    async with app.open_async():
        assert await enqueue_event_syncs(ix.db, app) >= 1
        assert await defer_event_sync(app, workspace_id=ix.ws, connection_id=ix.cid) is None
    [(name, queue, args)] = await _jobs(ix)
    assert (name, queue) == (SYNC_EVENT_REMINDERS, NOTIFY_QUEUE)
    assert NOTIFY_QUEUE == "notify" and args == {
        "workspace_id": str(ix.ws),
        "connection_id": str(ix.cid),
    }
    await purge_jobs(ix.db, ix.ws)


async def test_worker_runs_the_sync_job_and_creates_reminders(ix: IndexEnv, app_url: str) -> None:
    await add_event(ix, "합성 회의", NOW + 30 * MIN)
    app = build_worker_app(app_url, _deps(ix))
    await purge_jobs(ix.db, ix.ws)
    async with app.open_async():
        await defer_event_sync(app, workspace_id=ix.ws, connection_id=ix.cid)
        await WorkerPool(app, wait=False, install_signal_handlers=False, listen_notify=False).run()
    assert [r["title"] for r in await rows(ix)] == ["합성 회의"]
    await purge_jobs(ix.db, ix.ws)


class _Gated:
    """`list_events`에서 멈췄다가 풀어 주면 이어가는 느린 캘린더."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def list_events(self, *args: Any, **kwargs: Any) -> Any:
        self.started.set()
        await self.gate.wait()
        return await self._inner.list_events(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_slow_notify_job_does_not_block_other_queues(ix: IndexEnv, app_url: str) -> None:
    from orbit.runflow.worker import RUN_QUEUE

    await add_event(ix, "합성 회의", NOW + 30 * MIN)
    slow = _Gated(ix.conns.calendar[ACCOUNT])
    app = build_worker_app(app_url, _deps(ix, slow))

    # run 큐에 해당하는 작업이 끝나는지 보기 위해 가벼운 작업을 `default` 큐에 하나 둔다.
    seen: list[str] = []

    @app.task(name="probe_default", queue=RUN_QUEUE)
    async def probe_default() -> None:
        seen.append("default")

    await purge_jobs(ix.db, ix.ws)
    pool = WorkerPool(app, install_signal_handlers=False, listen_notify=False)
    async with app.open_async():
        await defer_event_sync(app, workspace_id=ix.ws, connection_id=ix.cid)
        running = asyncio.create_task(pool.run())
        try:
            await asyncio.wait_for(slow.started.wait(), 10)  # 알림 동기화가 멈춰 있다
            await probe_default.defer_async()
            for _ in range(100):  # 알림 작업이 도는 동안에도 default 큐 작업이 끝난다
                if seen:
                    break
                await asyncio.sleep(0.1)
            assert seen == ["default"]
            assert await rows(ix) == []  # 알림 동기화는 아직 끝나지 않았다
            slow.gate.set()
            for _ in range(100):
                if await rows(ix):
                    break
                await asyncio.sleep(0.1)
            assert [r["title"] for r in await rows(ix)] == ["합성 회의"]
        finally:
            slow.gate.set()
            pool.stop()
            await asyncio.wait_for(running, 15)
    await purge_jobs(ix.db, ix.ws)


# ---------------------------------------------------------------- 쓰기 직전 설정 재확인 (동시 변경)


class _ChangesSettingDuringFetch:
    """`list_events`를 부르는 사이 사용자가 설정을 바꾼 상황을 만든다."""

    def __init__(self, ix: IndexEnv, inner: Any, **change: Any) -> None:
        self._ix = ix
        self._inner = inner
        self._change = change

    async def list_events(self, *args: Any, **kwargs: Any) -> Any:
        found = await self._inner.list_events(*args, **kwargs)
        await service.save_settings(self._ix.db, self._ix.clock, self._ix.ws, **self._change)
        return found

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_turning_off_during_the_fetch_prevents_recreating_reminders(ix: IndexEnv) -> None:
    await add_event(ix, "합성 회의", NOW + 30 * MIN)
    racing = _ChangesSettingDuringFetch(
        ix, ix.conns.calendar[ACCOUNT], enabled=False, lead_minutes=10
    )
    result = await sync(ix, racing)
    assert result.outcome == "disabled"
    assert await rows(ix) == []


async def test_lead_changed_during_the_fetch_is_used_for_due_at(ix: IndexEnv) -> None:
    await add_event(ix, "합성 회의", NOW + 60 * MIN)
    racing = _ChangesSettingDuringFetch(
        ix, ix.conns.calendar[ACCOUNT], enabled=True, lead_minutes=30
    )
    await sync(ix, racing)
    [row] = await rows(ix)
    assert row["due_at"] == NOW + 30 * MIN


# ---------------------------------------------------------------- 보존 기간 (ADR-0021 §7)


async def _insert_event_row(ix: IndexEnv, ws: uuid.UUID, cid: uuid.UUID, start: Any) -> None:
    async with ix.db.workspace_session(ws) as s:
        await s.execute(
            text(
                "INSERT INTO notification (id, workspace_id, kind, title, due_at, status,"
                " created_at, connection_id, calendar_id, event_id, event_start)"
                " VALUES (:id, :ws, 'event_reminder', 'e', :due, 'scheduled', :now, :cid,"
                " 'c', :eid, :start)"
            ),
            {
                "id": ix.ids.next(),
                "ws": ws,
                "due": start - 10 * MIN,
                "now": NOW,
                "cid": cid,
                "eid": str(start),
                "start": start,
            },
        )


async def test_sync_purges_expired_notifications_of_its_workspace(ix: IndexEnv) -> None:
    from orbit.notify.limits import EVENT_REMINDER_RETENTION, REMINDER_RETENTION

    await _insert_event_row(ix, ix.ws, ix.cid, NOW - EVENT_REMINDER_RETENTION - MIN)
    await _insert_event_row(ix, ix.ws, ix.cid, NOW - EVENT_REMINDER_RETENTION + MIN)
    await insert_notification(ix, "합성 오래된 리마인더", NOW - REMINDER_RETENTION - MIN)
    await insert_notification(ix, "합성 최근 리마인더", NOW - REMINDER_RETENTION + MIN)
    await insert_notification(ix, "합성 예정 리마인더", NOW + 5 * MIN)
    await sync(ix)
    titles = sorted(r["title"] for r in await rows(ix))
    assert titles == ["e", "합성 예정 리마인더", "합성 최근 리마인더"]


async def test_periodic_purge_covers_workspaces_without_connection_or_with_reminders_off(
    ix: IndexEnv, async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    from orbit.notify.jobs import purge_expired_notifications
    from orbit.notify.limits import REMINDER_RETENTION

    old = NOW - REMINDER_RETENTION - timedelta(days=1)
    no_connection_ws = workspaces.create()  # 연결이 전혀 없는 워크스페이스
    await insert_notification(ix, "합성 연결 없음", old, ws=no_connection_ws)
    await insert_notification(ix, "합성 연결 없음 최근", NOW, ws=no_connection_ws)
    await insert_notification(ix, "합성 끈 워크스페이스", old)
    await service.save_settings(ix.db, ix.clock, ix.ws, enabled=False, lead_minutes=10)
    await disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
    assert (ix.ws, ix.cid) not in await ix.db.unscoped_event_reminder_due_connections()

    expired = await ix.db.unscoped_notification_expired_workspaces(
        NOW - timedelta(days=7), NOW - REMINDER_RETENTION
    )
    assert {ix.ws, no_connection_ws} <= set(expired)
    assert await purge_expired_notifications(ix.db, ix.clock) >= 2
    assert await rows(ix) == []
    async with ix.db.workspace_session(no_connection_ws) as s:
        left = (await s.execute(text("SELECT title FROM notification"))).scalars().all()
    assert left == ["합성 연결 없음 최근"]
    assert await purge_expired_notifications(ix.db, ix.clock) == 0  # 멱등


async def test_expired_function_returns_ids_only(ix: IndexEnv, raw_engine: Engine) -> None:
    with raw_engine.connect() as conn:
        cols = conn.execute(
            text("SELECT * FROM notification_expired_workspaces(:a, :a)"), {"a": NOW}
        )
        assert list(cols.keys()) == ["workspace_id"]
        conn.rollback()


async def test_tick_registers_syncs_even_when_purge_fails(
    ix: IndexEnv, app_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("SYNTH-SECRET-TEXT")

    monkeypatch.setattr("orbit.runflow.worker.purge_expired_notifications", boom)
    app = build_worker_app(app_url, _deps(ix))
    await purge_jobs(ix.db, ix.ws)
    caplog.set_level("WARNING")
    async with app.open_async():
        await app.tasks["schedule_event_reminders"].func(timestamp=0)
    assert len(await _jobs(ix)) == 1  # 정리가 실패해도 동기화는 등록됐다
    assert "RuntimeError" in caplog.text and "SYNTH-SECRET-TEXT" not in caplog.text
    await purge_jobs(ix.db, ix.ws)
