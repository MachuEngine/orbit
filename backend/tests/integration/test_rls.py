"""워크스페이스 격리 통합 테스트 (ADR-0004, 스파이크 R2·R4·R6 이식). 실제 PostgreSQL이 필요하다."""

import asyncio
import threading
import uuid

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, InvalidRequestError
from sqlalchemy.orm import Session

from integration.support import (
    CLOCK,
    IDS,
    INSERT_CONVERSATION,
    INSERT_MESSAGE,
    WorkspaceFactory,
)
from orbit.db.session import AsyncDatabase, SyncDatabase

COUNT_CONV = text("SELECT count(*) FROM conversation")
WS_IDS = text("SELECT DISTINCT workspace_id FROM conversation")
SET_WS = text("SELECT set_config('app.workspace_id', :id, true)")


def _assert_rls_violation(exc: DBAPIError) -> None:
    assert getattr(exc.orig, "sqlstate", None) == "42501"
    assert "row-level security policy" in str(exc.orig)


# (a) 설정 없음 ----------------------------------------------------------------


def test_without_setting_reads_zero_and_insert_denied(
    workspaces: WorkspaceFactory, raw_engine: Engine
) -> None:
    ws = workspaces.create(conversations=3)

    with raw_engine.connect() as conn:
        assert conn.execute(COUNT_CONV).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM workspace")).scalar_one() == 0

        with pytest.raises(DBAPIError) as info, conn.begin_nested():
            conn.execute(INSERT_CONVERSATION, {"id": IDS.next(), "ws": ws, "at": CLOCK.now()})
        _assert_rls_violation(info.value)

        # 대조군: 같은 연결에서 설정 후에는 삽입과 조회가 된다.
        conn.execute(SET_WS, {"id": str(ws)})
        conn.execute(INSERT_CONVERSATION, {"id": IDS.next(), "ws": ws, "at": CLOCK.now()})
        assert conn.execute(COUNT_CONV).scalar_one() == 4
        conn.rollback()


# (b) A 설정에서 B 데이터 접근 --------------------------------------------------


def test_workspace_a_cannot_write_or_touch_b(
    workspaces: WorkspaceFactory, sync_db: SyncDatabase
) -> None:
    a = workspaces.create(conversations=1)
    b = workspaces.create(conversations=1)
    with sync_db.workspace_session(b) as s:
        b_conv = s.execute(text("SELECT id FROM conversation")).scalar_one()

    with sync_db.workspace_session(a) as s:
        a_conv = s.execute(text("SELECT id FROM conversation")).scalar_one()
        assert (
            s.execute(
                text("SELECT count(*) FROM conversation WHERE id = :id"), {"id": b_conv}
            ).scalar_one()
            == 0
        )

        # 삽입: 다른 워크스페이스 ID로는 WITH CHECK에서 거부된다.
        with pytest.raises(DBAPIError) as info, s.begin_nested():
            s.execute(INSERT_CONVERSATION, {"id": IDS.next(), "ws": b, "at": CLOCK.now()})
        _assert_rls_violation(info.value)

        # 수정: B의 행은 보이지 않아 0행이 바뀐다.
        upd = s.execute(
            text("UPDATE conversation SET created_at = :at WHERE id = :id"),
            {"at": CLOCK.now(), "id": b_conv},
        )
        assert upd.rowcount == 0  # type: ignore[attr-defined]

        # 수정으로 자기 행을 B로 옮기는 것도 WITH CHECK가 막는다.
        with pytest.raises(DBAPIError) as info2, s.begin_nested():
            s.execute(
                text("UPDATE conversation SET workspace_id = :b WHERE id = :id"),
                {"b": b, "id": a_conv},
            )
        _assert_rls_violation(info2.value)

        # 삭제: 0행.
        dele = s.execute(text("DELETE FROM conversation WHERE id = :id"), {"id": b_conv})
        assert dele.rowcount == 0  # type: ignore[attr-defined]

        # 다른 워크스페이스 대화에 메시지를 붙일 수 없다. workspace_id가 A라서 RLS는 통과하지만
        # 복합 FK (conversation_id, workspace_id)가 막는다(sqlstate 23503).
        with pytest.raises(DBAPIError) as fk_info, s.begin_nested():
            s.execute(INSERT_MESSAGE, _message(a, b_conv))
        assert getattr(fk_info.value.orig, "sqlstate", None) == "23503"

    # B의 데이터는 그대로다.
    with sync_db.workspace_session(b) as s:
        assert s.execute(COUNT_CONV).scalar_one() == 1


def _message(ws: uuid.UUID, conv: uuid.UUID) -> dict[str, object]:
    return {"id": IDS.next(), "ws": ws, "conv": conv, "content": "synthetic", "at": CLOCK.now()}


COUNT_MSG = text("SELECT count(*) FROM message")
COUNT_WS = text("SELECT count(*) FROM workspace")


def test_message_and_workspace_policies(
    workspaces: WorkspaceFactory, sync_db: SyncDatabase
) -> None:
    a = workspaces.create(conversations=1)
    b = workspaces.create(conversations=1)
    with sync_db.workspace_session(b) as s:
        b_conv = s.execute(text("SELECT id FROM conversation")).scalar_one()
        s.execute(INSERT_MESSAGE, _message(b, b_conv))

    with sync_db.workspace_session(a) as s:
        a_conv = s.execute(text("SELECT id FROM conversation")).scalar_one()

        # 대조군: 같은 워크스페이스의 메시지는 삽입하고 조회할 수 있다.
        s.execute(INSERT_MESSAGE, _message(a, a_conv))
        assert s.execute(COUNT_MSG).scalar_one() == 1
        assert s.execute(text("SELECT DISTINCT workspace_id FROM message")).scalar_one() == a

        # B의 message는 보이지 않는다.
        assert (
            s.execute(
                text("SELECT count(*) FROM message WHERE workspace_id = :b"), {"b": b}
            ).scalar_one()
            == 0
        )

        # workspace_id = B로 message 삽입: WITH CHECK 거부.
        with pytest.raises(DBAPIError) as info, s.begin_nested():
            s.execute(INSERT_MESSAGE, _message(b, b_conv))
        _assert_rls_violation(info.value)

        # workspace 정책: 자기 행만 보이고, B id로는 삽입할 수 없다.
        assert s.execute(COUNT_WS).scalar_one() == 1
        assert s.execute(text("SELECT id FROM workspace")).scalar_one() == a
        with pytest.raises(DBAPIError) as ws_info, s.begin_nested():
            s.execute(
                text("INSERT INTO workspace (id, created_at) VALUES (:id, :at)"),
                {"id": b, "at": CLOCK.now()},
            )
        _assert_rls_violation(ws_info.value)

    with sync_db.workspace_session(b) as s:
        assert s.execute(COUNT_MSG).scalar_one() == 1


# (b-2) 블록 안에서 commit/rollback을 직접 부른 경우 ------------------------------------
#
# 실제 동작(SQLAlchemy 2.1.8): `workspace_session`이 `session.begin()` 컨텍스트 안에 있어서
#   - commit(): 그 시점까지의 변경을 커밋한다(호출 자체는 성공한다).
#   - 이후 session.execute(...): InvalidRequestError ("Can't operate on closed transaction
#     inside context manager")로 실패한다. 설정 없는 새 트랜잭션이 열리지 않는다.
# 어느 경우든 워크스페이스 설정 없이 데이터가 보이면 안 된다.


def _no_unscoped_visibility_sync(s: Session) -> str:
    try:
        count = s.execute(COUNT_CONV).scalar_one()
    except InvalidRequestError as exc:
        assert "closed transaction" in str(exc)
        return "raised"
    assert count == 0, "블록 안 commit/rollback 뒤에 설정 없이 데이터가 보였다"
    return "zero"


@pytest.mark.parametrize("action", ["commit", "rollback"])
def test_manual_commit_or_rollback_inside_block_never_exposes_rows(
    action: str, workspaces: WorkspaceFactory, sync_db: SyncDatabase
) -> None:
    ws = workspaces.create(conversations=2)
    with sync_db.workspace_session(ws) as s:
        assert s.execute(COUNT_CONV).scalar_one() == 2
        getattr(s, action)()
        outcome = _no_unscoped_visibility_sync(s)
    assert outcome in {"raised", "zero"}
    # 블록을 정상 종료해도 다음 요청에는 설정이 남지 않는다.
    with sync_db.workspace_session(ws) as s:
        assert s.execute(COUNT_CONV).scalar_one() == 2


@pytest.mark.parametrize("action", ["commit", "rollback"])
async def test_manual_commit_or_rollback_inside_block_never_exposes_rows_async(
    action: str, app_url: str, workspaces: WorkspaceFactory
) -> None:
    ws = workspaces.create(conversations=2)
    db = AsyncDatabase.from_url(app_url)
    try:
        async with db.workspace_session(ws) as s:
            assert (await s.execute(COUNT_CONV)).scalar_one() == 2
            await getattr(s, action)()
            try:
                count = (await s.execute(COUNT_CONV)).scalar_one()
            except InvalidRequestError as exc:
                assert "closed transaction" in str(exc)
            else:
                assert count == 0, "블록 안 commit/rollback 뒤에 설정 없이 데이터가 보였다"
        async with db.workspace_session(ws) as s:
            assert (await s.execute(COUNT_CONV)).scalar_one() == 2
    finally:
        await db.dispose()


# (c) 풀 재사용 ------------------------------------------------------------------


# 블록이 끝나는 세 가지 방식. 풀 크기 1이라 다음 요청이 같은 연결(pid)을 받는다.
#   commit:    블록을 정상 종료한다(헬퍼가 커밋).
#   rollback:  블록 안에서 s.rollback()을 직접 부른 뒤 정상 종료한다.
#   exception: 블록 안에서 예외가 나 헬퍼가 롤백한다.
@pytest.mark.parametrize("ending", ["commit", "rollback", "exception"])
def test_pool_size_one_next_request_sees_zero_rows(
    ending: str, app_url: str, workspaces: WorkspaceFactory
) -> None:
    ws = workspaces.create(conversations=2)

    # 같은 풀을 헬퍼와 설정 없는 조회가 나눠 쓰도록 엔진을 직접 만들어 주입한다.
    engine = create_engine(app_url, pool_size=1, max_overflow=0)
    db = SyncDatabase(engine)
    try:
        pid_in_scope = None
        try:
            with db.workspace_session(ws) as s:
                pid_in_scope = s.execute(text("SELECT pg_backend_pid()")).scalar_one()
                assert s.execute(COUNT_CONV).scalar_one() == 2
                if ending == "rollback":
                    # 블록 안에서 직접 rollback한 뒤 정상 종료하는 경우다(커밋·롤백 후 연결 반납).
                    s.rollback()
                elif ending == "exception":
                    raise RuntimeError("synthetic failure")
        except RuntimeError:
            assert ending == "exception"

        with engine.connect() as conn:
            pid_next = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            assert pid_next == pid_in_scope  # 같은 연결을 재사용했다
            assert conn.execute(COUNT_CONV).scalar_one() == 0
            assert conn.execute(
                text("SELECT current_setting('app.workspace_id', true)")
            ).scalar_one() in (
                None,
                "",
            )
    finally:
        db.dispose()


# (d) 동시성 ---------------------------------------------------------------------

ITERATIONS = 150
WORKERS = 8


def test_threads_alternating_workspaces_never_cross(
    app_url: str, workspaces: WorkspaceFactory
) -> None:
    a = workspaces.create(conversations=3)
    b = workspaces.create(conversations=3)
    engine = create_engine(app_url, pool_size=3, max_overflow=0)
    db = SyncDatabase(engine)
    violations: list[str] = []
    errors: list[BaseException] = []

    def worker(offset: int) -> None:
        try:
            for i in range(ITERATIONS):
                ws = a if (i + offset) % 2 == 0 else b
                with db.workspace_session(ws) as s:
                    seen = {row[0] for row in s.execute(WS_IDS)}
                    if seen != {ws}:
                        violations.append(f"scoped {ws}: saw {seen}")
                if i % 5 == 0:
                    with engine.connect() as conn:  # 설정 없는 조회는 항상 0행
                        n = conn.execute(COUNT_CONV).scalar_one()
                        if n != 0:
                            violations.append(f"unscoped saw {n} rows")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(WORKERS)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        db.dispose()
    assert not errors, errors
    assert violations == []


async def test_async_tasks_alternating_workspaces_never_cross(
    app_url: str, workspaces: WorkspaceFactory
) -> None:
    a = workspaces.create(conversations=3)
    b = workspaces.create(conversations=3)
    db = AsyncDatabase.from_url(app_url, pool_size=3, max_overflow=0)
    violations: list[str] = []

    async def worker(offset: int) -> None:
        for i in range(ITERATIONS):
            ws: uuid.UUID = a if (i + offset) % 2 == 0 else b
            async with db.workspace_session(ws) as s:
                seen = {row[0] for row in await s.execute(WS_IDS)}
                if seen != {ws}:
                    violations.append(f"scoped {ws}: saw {seen}")
            await asyncio.sleep(0)

    try:
        await asyncio.gather(*(worker(k) for k in range(WORKERS)))
    finally:
        await db.dispose()
    assert violations == []


# (e) 역할 -----------------------------------------------------------------------


def test_connects_as_non_owner_without_bypassrls(sync_db: SyncDatabase) -> None:
    with sync_db.workspace_session(IDS.next()) as s:
        role = s.execute(
            text(
                "SELECT r.rolname, r.rolsuper, r.rolbypassrls FROM pg_roles r"
                " WHERE r.rolname = current_user"
            )
        ).one()
        assert role.rolname == "orbit_app"
        assert role.rolsuper is False
        assert role.rolbypassrls is False

        owners = s.execute(
            text(
                "SELECT tablename, tableowner, rowsecurity FROM pg_tables"
                " WHERE schemaname = 'public'"
                " AND tablename IN ('workspace', 'conversation', 'message')"
            )
        ).all()
        assert {o.tablename for o in owners} == {"workspace", "conversation", "message"}
        assert all(o.tableowner != "orbit_app" for o in owners)
        assert all(o.rowsecurity for o in owners)

        forced = s.execute(
            text(
                "SELECT relname FROM pg_class"
                " WHERE relname IN ('workspace', 'conversation', 'message')"
                " AND relforcerowsecurity"
            )
        ).all()
        assert {f.relname for f in forced} == {"workspace", "conversation", "message"}
