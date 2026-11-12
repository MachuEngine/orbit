"""메일 색인: 동기화·범위·삭제 전파·검색·RLS·작업 등록 (ADR-0019). 데이터는 모두 합성 값이다."""

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from integration.mailindex_support import (
    EXTRAS,
    NOW,
    IndexEnv,
    add_inbox,
    build_env,
    mail,
    purge_jobs,
)
from integration.runflow_support import StubResolver
from integration.support import WorkspaceFactory
from orbit.connections import store as connection_store
from orbit.connections.oauth import DATA_SCOPES, ConnectGrant
from orbit.connections.service import disconnect, save_grant
from orbit.connectors.contracts import PermissionRevoked, TransientError
from orbit.crypto.envelope import EnvelopeCipher, KeyEncryptionKey
from orbit.db.session import AsyncDatabase
from orbit.mailindex import service, store
from orbit.mailindex.jobs import SYNC_MAIL_INDEX, defer_sync, enqueue_due_syncs
from orbit.mailindex.search import search_index
from orbit.mailindex.store import purge_connection
from orbit.mailindex.sync import sync_connection
from orbit.records.jobs import make_procrastinate_app
from orbit.runflow.deps import RunDeps
from orbit.runflow.worker import WorkerPool, build_worker_app

FOUR_TABLES = ("index_scope", "sync_state", "source_item", "chunk")

# ---------------------------------------------------------------- 첫 동기화·증분·만료


async def test_first_sync_indexes_only_labeled_mail_and_records_state(ix: IndexEnv) -> None:
    await add_inbox(ix)
    result = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert (result.outcome, result.mode) == ("synced", "full")
    assert await ix.item_ids() == {"msg-a-001", "msg-a-003", "m-quote", "m-price", "m-lunch"}
    assert "msg-a-002" not in await ix.item_ids()  # SENT 메일은 범위 밖
    status = await service.get_status(ix.mi, ix.ws, ix.cid)
    assert status.status == "ok" and status.last_synced_at == NOW
    assert status.indexed_mail_count == 5 and status.error_kind is None
    assert [s.initial_synced_at for s in status.scopes] == [NOW]
    assert await ix.scalar("SELECT count(*) FROM chunk WHERE embedding_model = 'fake-embedding-v1'")
    assert await ix.scalar("SELECT history_id::int FROM sync_state") == 0


async def test_unchanged_mail_is_not_embedded_again(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    first = ix.embedder.embedded
    assert first > 0
    again = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert (again.outcome, again.mode, again.embedded_chunks) == ("synced", "incremental", 0)
    ix.state.expire_history()  # 이력 만료 -> 전체 동기화로 모든 메일을 다시 읽는다
    full = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert (full.mode, full.fetched, full.embedded_chunks) == ("full", 5, 0)
    assert ix.embedder.embedded == first  # 본문 해시가 같으니 임베딩은 늘지 않았다


async def test_changed_body_is_embedded_again(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    before = ix.embedder.embedded
    ix.state.messages["m-lunch"] = mail("m-lunch", "점심 메뉴 안내", "이번 주는 비빔밥입니다.")
    ix.state.record_history("labels_added", "m-lunch")  # 이력에 변경이 잡히게 한다
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert ix.embedder.embedded == before + 1
    hits = await ix.search("비빔밥")
    assert hits[0].provider_item_id == "m-lunch"


async def test_incremental_sync_handles_add_delete_and_label_changes(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    ix.state.add_message(mail("m-new", "신규 계약서 검토", "계약서 초안을 검토해 주세요."))
    ix.state.delete_message("m-lunch")
    ix.state.set_labels("m-price", remove=("INBOX",))  # 범위 라벨이 모두 빠졌다
    ix.state.set_labels("m-quote", add=("Label_9",))  # 범위 밖 라벨이 붙었을 뿐이다
    result = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert (result.outcome, result.mode) == ("synced", "incremental")
    assert await ix.item_ids() == {"msg-a-001", "msg-a-003", "m-quote", "m-new"}
    # 라벨 목록도 갱신된다.
    async with ix.db.workspace_session(ix.ws) as s:
        labels = (
            await s.execute(
                text("SELECT label_ids FROM source_item WHERE provider_item_id = 'm-quote'")
            )
        ).scalar_one()
    assert set(labels) == {"INBOX", "Label_9"}
    # 지운 메일의 청크도 남지 않는다.
    assert (
        await ix.scalar(
            "SELECT count(*) FROM chunk c LEFT JOIN source_item s ON s.id = c.source_item_id"
            " WHERE s.id IS NULL"
        )
        == 0
    )
    assert not await ix.search("김밥 우동", vector=False)


async def test_history_expiry_falls_back_to_full_sync_and_drops_missing_items(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    del ix.state.messages["m-lunch"]  # 이력 없이 사라졌다
    ix.state.threads["thr-m-lunch"].clear()
    ix.state.add_message(mail("m-late", "늦게 온 메일", "합성 본문입니다."))
    ix.state.expire_history()
    result = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert (result.outcome, result.mode) == ("synced", "full")
    ids = await ix.item_ids()
    assert "m-lunch" not in ids and "m-late" in ids
    assert result.deleted == 1


async def test_period_filter_keeps_old_mail_out(ix: IndexEnv) -> None:
    ix.state.add_message(
        mail("m-old", "오래된 견적", "오래된 합성 본문", sent_at=NOW - timedelta(days=60))
    )
    await add_inbox(ix, days=30)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert "m-old" not in await ix.item_ids() and "m-quote" in await ix.item_ids()
    # 기간을 늘리면 그 범위를 다시 읽는다(증분 동기화가 아니라 새 범위 읽기).
    await add_inbox(ix, days=90)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert "m-old" in await ix.item_ids()


async def test_new_scope_is_read_on_next_sync_without_touching_history(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    await service.add_scope(
        ix.mi, ix.ws, ix.cid, label_id="SENT", label_name="SENT", period_days=365
    )
    result = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert result.outcome == "synced" and "msg-a-002" in await ix.item_ids()


async def test_connector_error_is_recorded_without_losing_the_index(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)

    class Revoked:
        def __getattr__(self, name: str) -> object:
            async def fail(*a: object, **k: object) -> None:
                raise PermissionRevoked("합성", reason="invalid_grant")

            return fail

    result = await sync_connection(ix.mi, Revoked(), ix.ws, ix.cid)  # type: ignore[arg-type]
    assert (result.outcome, result.error_kind) == ("error", "permission_revoked")
    status = await service.get_status(ix.mi, ix.ws, ix.cid)
    assert status.status == "error" and status.error_kind == "permission_revoked"
    assert status.last_synced_at == NOW and status.indexed_mail_count == 5


async def test_sync_is_quiet_for_missing_or_revoked_connection_and_no_scope(ix: IndexEnv) -> None:
    assert (await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)).outcome == "no_scopes"
    assert (await sync_connection(ix.mi, ix.mail, ix.ws, uuid.uuid4())).outcome == "no_connection"
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    await disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
    assert (await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)).outcome == "no_connection"
    assert await ix.scalar("SELECT count(*) FROM source_item") == 0


async def test_same_connection_sync_is_serialized_by_advisory_lock(ix: IndexEnv) -> None:
    await add_inbox(ix)
    async with ix.db.workspace_session(ix.ws) as holder:
        got = await holder.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"mail_index_sync:{ix.cid}"},
        )
        assert got.scalar_one() is True
        busy = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
        assert busy.outcome == "busy" and await ix.scalar("SELECT count(*) FROM source_item") == 0
    assert (await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)).outcome == "synced"


# ---------------------------------------------------------------- 범위·삭제 전파


async def test_removing_a_scope_prunes_items_outside_the_rest(ix: IndexEnv) -> None:
    await add_inbox(ix)
    sent = await service.add_scope(
        ix.mi, ix.ws, ix.cid, label_id="SENT", label_name="SENT", period_days=30
    )
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert "msg-a-002" in await ix.item_ids()
    assert await service.remove_scope(ix.mi, ix.ws, ix.cid, sent.id) is True
    assert "msg-a-002" not in await ix.item_ids() and "m-quote" in await ix.item_ids()
    assert await service.remove_scope(ix.mi, ix.ws, ix.cid, sent.id) is False  # 이미 없다
    [inbox] = (await service.get_status(ix.mi, ix.ws, ix.cid)).scopes
    assert await service.remove_scope(ix.mi, ix.ws, ix.cid, inbox.id) is True
    for table in ("source_item", "chunk", "sync_state", "index_scope"):
        assert await ix.scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    assert (await service.get_status(ix.mi, ix.ws, ix.cid)).status == "none"


async def test_scope_validation_and_inactive_connection(ix: IndexEnv) -> None:
    with pytest.raises(service.InvalidScope):
        await service.add_scope(
            ix.mi, ix.ws, ix.cid, label_id="INBOX", label_name="x", period_days=7
        )
    with pytest.raises(service.InvalidScope):
        await service.add_scope(ix.mi, ix.ws, ix.cid, label_id=" ", label_name="x", period_days=30)
    with pytest.raises(service.ConnectionNotActive):
        await service.add_scope(
            ix.mi, ix.ws, uuid.uuid4(), label_id="INBOX", label_name="x", period_days=30
        )


async def test_disconnect_leaves_zero_rows_in_the_four_tables(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    for table in FOUR_TABLES:
        assert await ix.scalar(f"SELECT count(*) FROM {table}") > 0  # noqa: S608
    result = await disconnect(
        ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock
    )
    assert result.found and result.status == "revoked"
    for table in FOUR_TABLES:
        assert await ix.scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    assert not await ix.search("견적")


async def test_disconnect_does_not_touch_another_connections_index(
    ix: IndexEnv, async_db: AsyncDatabase, workspaces: WorkspaceFactory, owner_engine: Engine
) -> None:
    other = await build_env(async_db, workspaces, owner_engine, extra=EXTRAS)
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    await add_inbox(other)
    await sync_connection(other.mi, other.mail, other.ws, other.cid)
    await disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
    assert await other.scalar("SELECT count(*) FROM chunk") > 0
    assert await other.scalar("SELECT count(*) FROM source_item") == 5


# ---------------------------------------------------------------- 검색


async def test_keyword_search_finds_korean_content_words(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    # 조사·어미가 달라도 같은 내용어로 맞는다. Kiwi는 '견적서'를 한 단어로 보므로 '견적'과는 다르다.
    hits = await ix.search("견적서에 대해 얘기한 메일", vector=False)
    assert {h.provider_item_id for h in hits} == {"m-quote"}
    assert all(h.score > 0 and h.excerpt for h in hits)
    assert {h.provider_item_id for h in await ix.search("단가를 낮춰", vector=False)} == {"m-price"}
    assert {h.provider_item_id for h in await ix.search("견적", vector=False)} == {"m-price"}


async def test_vector_only_search_when_query_has_no_content_words(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    hits = await ix.search("?!", keyword=False)
    assert len(hits) == 5  # 벡터 후보는 가장 가까운 순으로 모두 나온다
    assert not await ix.search("?!", vector=False)  # 둘 다 없으면 빈 결과
    ranked = await ix.search("점심 김밥 우동", keyword=False)
    assert ranked[0].provider_item_id == "m-lunch"


async def test_hybrid_search_ranks_matching_mail_first_and_groups_by_mail(ix: IndexEnv) -> None:
    long_body = "계약 조건 검토. " * 200 + "견적 단가 협의 내용"
    ix.state.add_message(mail("m-long", "긴 메일", long_body))
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert (
        await ix.scalar(
            "SELECT count(*) FROM chunk WHERE source_item_id IN"
            " (SELECT id FROM source_item WHERE provider_item_id = 'm-long')"
        )
        > 1
    )
    hits = await ix.search("견적 단가 협의", limit=10)
    ids = [h.provider_item_id for h in hits]
    assert len(ids) == len(set(ids))  # 메일 단위로 묶였다
    assert set(ids[:3]) >= {"m-quote", "m-price"} and ids.index("m-lunch") > ids.index("m-price")
    assert hits[0].score >= hits[-1].score
    assert len(await ix.search("견적", limit=2)) == 2


async def due_connections(
    ix: IndexEnv, stale_before: datetime
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    return await ix.db.unscoped_mail_index_due_connections(
        stale_before, ix.clock.now() - ix.mi.error_backoff
    )


async def setting(session: AsyncSession) -> str | None:
    value = (
        await session.execute(text("SELECT current_setting('hnsw.iterative_scan', true)"))
    ).scalar_one()
    return None if value is None else str(value)


async def test_search_turns_on_iterative_hnsw_scan_for_the_transaction(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    embedding = (await ix.embedder.embed(["견적"]))[0]
    async with ix.db.workspace_session(ix.ws) as s:
        assert await setting(s) != "relaxed_order"
        await search_index(s, ix.ws, embedding=embedding, tokens=["견적"], limit=3)
        assert await setting(s) == "relaxed_order"
    async with ix.db.workspace_session(ix.ws) as s:  # SET LOCAL이라 트랜잭션 밖으로 새지 않는다
        assert await setting(s) != "relaxed_order"


async def test_search_ignores_chunks_of_inactive_connections(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert await ix.search("견적")
    async with ix.db.workspace_session(ix.ws) as s:
        await s.execute(
            text(
                "UPDATE connection SET status = 'error', token_ciphertext = NULL,"
                " token_nonce = NULL, wrapped_dek = NULL, kek_id = NULL WHERE id = :id"
            ),
            {"id": ix.cid},
        )
    assert await ix.scalar("SELECT count(*) FROM chunk") > 0
    assert await ix.search("견적") == []


async def test_index_uses_hnsw_and_gin_indexes(ix: IndexEnv) -> None:
    async with ix.db.workspace_session(ix.ws) as s:
        defs = {
            r[0]: r[1]
            for r in await s.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunk'")
            )
        }
    assert "USING hnsw (embedding vector_cosine_ops)" in defs["chunk_embedding_hnsw"]
    assert "USING gin (tokens)" in defs["chunk_tokens_gin"]


# ---------------------------------------------------------------- RLS


async def test_other_workspaces_chunks_never_appear_and_unset_workspace_sees_nothing(
    ix: IndexEnv,
    async_db: AsyncDatabase,
    workspaces: WorkspaceFactory,
    owner_engine: Engine,
    raw_engine: Engine,
) -> None:
    secret = mail("m-secret", "다른 워크스페이스 전용", "비밀스러운 합성 본문 보라색코끼리")
    other = await build_env(async_db, workspaces, owner_engine, extra=(secret,))
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    await add_inbox(other)
    await sync_connection(other.mi, other.mail, other.ws, other.cid)

    assert [h.provider_item_id for h in await other.search("보라색코끼리", vector=False)] == [
        "m-secret"
    ]
    assert await ix.search("보라색코끼리", vector=False) == []
    assert "m-secret" not in {h.provider_item_id for h in await ix.search("합성 본문", limit=20)}
    assert "m-secret" not in await ix.item_ids()

    # SQL에 다른 워크스페이스 ID를 직접 넘겨도 RLS가 막는다(두 번째 방어선).
    embedding = (await ix.embedder.embed(["보라색코끼리"]))[0]
    async with ix.db.workspace_session(ix.ws) as s:
        leaked = await search_index(
            s, other.ws, embedding=embedding, tokens=["보라색코끼리"], limit=20
        )
    assert leaked == []

    # 워크스페이스 설정이 없으면 네 테이블 모두 0행이다.
    with raw_engine.connect() as conn:
        for table in FOUR_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0  # noqa: S608
        conn.rollback()


async def test_rows_for_another_workspace_cannot_be_written(ix: IndexEnv) -> None:
    other_ws = uuid.uuid4()
    with pytest.raises(DBAPIError):
        async with ix.db.workspace_session(ix.ws) as s:
            await s.execute(
                text(
                    "INSERT INTO index_scope (id, workspace_id, connection_id, label_id,"
                    " label_name, period_days, created_at)"
                    " VALUES (:id, :ws, :cid, 'X', 'X', 30, :now)"
                ),
                {"id": uuid.uuid4(), "ws": other_ws, "cid": ix.cid, "now": NOW},
            )
    with pytest.raises(DBAPIError):  # 다른 워크스페이스 연결을 가리키는 행은 복합 FK가 막는다
        async with ix.db.workspace_session(ix.ws) as s:
            await s.execute(
                text(
                    "INSERT INTO index_scope (id, workspace_id, connection_id, label_id,"
                    " label_name, period_days, created_at)"
                    " VALUES (:id, :ws, :cid, 'X', 'X', 30, :now)"
                ),
                {"id": uuid.uuid4(), "ws": ix.ws, "cid": uuid.uuid4(), "now": NOW},
            )


# ---------------------------------------------------------------- 작업 등록·주기 작업


async def test_job_args_are_ids_only_and_pending_job_is_unique(ix: IndexEnv, app_url: str) -> None:
    ix.mi.jobs = make_procrastinate_app(app_url)
    await add_inbox(ix)
    await add_inbox(ix, days=90)  # 같은 연결의 대기 작업은 하나만 둔다
    assert await service.request_sync(ix.mi, ix.ws, ix.cid) is False
    async with ix.db.workspace_session(ix.ws) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT task_name, args FROM procrastinate_jobs"
                    " WHERE args->>'workspace_id' = :ws"
                ),
                {"ws": str(ix.ws)},
            )
        ).all()
    assert [(r[0], r[1]) for r in rows] == [
        (SYNC_MAIL_INDEX, {"workspace_id": str(ix.ws), "connection_id": str(ix.cid)})
    ]


async def test_defer_in_rolled_back_transaction_leaves_no_job(ix: IndexEnv, app_url: str) -> None:
    app = make_procrastinate_app(app_url)
    with pytest.raises(RuntimeError):
        async with ix.db.workspace_session(ix.ws) as s:
            await defer_sync(s, app, workspace_id=ix.ws, connection_id=ix.cid)
            raise RuntimeError("롤백")
    async with ix.db.workspace_session(ix.ws) as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM procrastinate_jobs WHERE args->>'workspace_id' = :ws"),
                {"ws": str(ix.ws)},
            )
        ).scalar_one()
    assert count == 0


async def test_worker_runs_sync_job_and_periodic_selection(ix: IndexEnv, app_url: str) -> None:
    deps = RunDeps(
        db=ix.db,
        clock=ix.clock,
        ids=ix.ids,
        llm=None,  # type: ignore[arg-type]
        resolver=StubResolver(ix.conns.calendar["acct-alice"], ix.mail),
        tz=None,  # type: ignore[arg-type]
        checkpoint_conninfo="",
        mail_index=ix.mi,
    )
    app = build_worker_app(app_url, deps)
    assert ix.mi.jobs is app
    await purge_jobs(ix.db, ix.ws)
    await add_inbox(ix)  # 범위를 저장하면 작업이 등록된다
    async with app.open_async():
        await WorkerPool(app, wait=False, install_signal_handlers=False, listen_notify=False).run()
    status = await service.get_status(ix.mi, ix.ws, ix.cid)
    assert status.status == "ok" and status.indexed_mail_count == 5

    # 주기 작업의 대상 선별: 방금 동기화했으면 대상이 아니고, 15분이 지나면 대상이다.
    due = await due_connections(ix, ix.clock.now() - ix.mi.sync_interval)
    assert (ix.ws, ix.cid) not in due
    ix.clock.advance(timedelta(minutes=16))
    due = await due_connections(ix, ix.clock.now() - ix.mi.sync_interval)
    assert (ix.ws, ix.cid) in due
    async with app.open_async():
        assert await enqueue_due_syncs(ix.mi, app) >= 1
        assert await enqueue_due_syncs(ix.mi, app) == 0  # 이미 대기 중이다
    await purge_jobs(ix.db, ix.ws)


async def test_due_selection_skips_revoked_connections_and_connections_without_scope(
    ix: IndexEnv,
) -> None:
    far = ix.clock.now() + timedelta(days=1)
    assert (ix.ws, ix.cid) not in await due_connections(ix, far)  # 범위 없음
    await add_inbox(ix)
    assert (ix.ws, ix.cid) in await due_connections(ix, far)  # 첫 동기화 전
    await disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
    assert (ix.ws, ix.cid) not in await due_connections(ix, far)


async def test_due_function_returns_ids_only_and_is_the_only_cross_workspace_read(
    ix: IndexEnv, raw_engine: Engine
) -> None:
    await add_inbox(ix)
    with raw_engine.connect() as conn:
        cols = conn.execute(
            text("SELECT * FROM mail_index_due_connections(:b, :b)"), {"b": NOW + timedelta(days=1)}
        )
        assert list(cols.keys()) == ["workspace_id", "connection_id"]
        conn.rollback()
        # 테이블을 직접 읽으면 워크스페이스 설정이 없을 때 0행이다.
        assert conn.execute(text("SELECT count(*) FROM index_scope")).scalar_one() == 0
        conn.rollback()


# ---------------------------------------------------------------- 보존 기간 (ADR-0019 §7)


async def test_shrinking_the_period_removes_items_pushed_out_on_next_sync(ix: IndexEnv) -> None:
    ix.state.add_message(
        mail("m-old", "두 달 전 메일", "오래된 합성 본문", sent_at=NOW - timedelta(days=60))
    )
    await add_inbox(ix, days=90)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert "m-old" in await ix.item_ids()
    await add_inbox(ix, days=30)  # 기간을 줄인다. 아직 정리되지 않는다.
    assert "m-old" in await ix.item_ids()
    result = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert "m-old" not in await ix.item_ids() and "m-quote" in await ix.item_ids()
    assert result.deleted == 1
    assert (
        await ix.scalar(
            "SELECT count(*) FROM chunk c LEFT JOIN source_item s ON s.id = c.source_item_id"
            " WHERE s.id IS NULL"
        )
        == 0
    )


async def test_retention_uses_the_longest_period_among_the_items_scopes(ix: IndexEnv) -> None:
    ix.state.add_message(
        mail(
            "m-both",
            "두 범위에 속한 메일",
            "합성 본문",
            labels=("INBOX", "Label_9"),
            sent_at=NOW - timedelta(days=60),
        )
    )
    await add_inbox(ix, days=30)
    nine = await service.add_scope(
        ix.mi, ix.ws, ix.cid, label_id="Label_9", label_name="L9", period_days=365
    )
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert "m-both" in await ix.item_ids()  # 30일 밖이지만 365일 범위에 속한다
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert "m-both" in await ix.item_ids()
    await service.remove_scope(ix.mi, ix.ws, ix.cid, nine.id)  # 남은 범위(30일) 밖이 된다
    assert "m-both" not in await ix.item_ids()


# ---------------------------------------------------------------- 주기 대상: 오류 백오프


class _Failing:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __getattr__(self, name: str) -> Any:
        async def fail(*a: object, **k: object) -> None:
            raise self._error

        return fail


async def test_periodic_selection_skips_permanent_errors_and_backs_off_other_errors(
    ix: IndexEnv,
) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    stale = ix.clock.now() + timedelta(days=1)  # 동기화 시각 기준으로는 항상 오래됐다
    assert (ix.ws, ix.cid) in await due_connections(ix, stale)

    ix.clock.advance(timedelta(minutes=20))
    transient = _Failing(TransientError("합성"))
    assert (await sync_connection(ix.mi, transient, ix.ws, ix.cid)).error_kind == "transient_error"
    assert (ix.ws, ix.cid) not in await due_connections(ix, stale)  # 방금 실패했다: 백오프
    ix.clock.advance(timedelta(minutes=59))
    assert (ix.ws, ix.cid) not in await due_connections(ix, stale)
    ix.clock.advance(timedelta(minutes=2))  # 마지막 시도 후 1시간 넘음
    assert (ix.ws, ix.cid) in await due_connections(ix, stale)

    revoked = _Failing(PermissionRevoked("합성", reason="invalid_grant"))
    assert (await sync_connection(ix.mi, revoked, ix.ws, ix.cid)).error_kind == "permission_revoked"
    ix.clock.advance(timedelta(days=3))
    assert (ix.ws, ix.cid) not in await due_connections(ix, ix.clock.now() + timedelta(days=1))
    # 성공하면 다시 대상이 된다.
    assert (await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)).outcome == "synced"
    assert (ix.ws, ix.cid) in await due_connections(ix, ix.clock.now() + timedelta(days=1))


# ---------------------------------------------------------------- 해제와의 경합


async def test_scope_save_waits_for_a_concurrent_disconnect_and_is_rejected(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    async with ix.db.workspace_session(ix.ws) as closing:
        # 해제 트랜잭션이 연결을 바꾸고 지운 채 아직 커밋하지 않았다.
        await connection_store.mark_dead(
            closing, ix.ws, ix.cid, status="revoked", now=ix.clock.now()
        )
        await purge_connection(closing, ix.ws, ix.cid)
        saving = asyncio.create_task(
            service.add_scope(
                ix.mi, ix.ws, ix.cid, label_id="SENT", label_name="SENT", period_days=30
            )
        )
        await asyncio.sleep(0.3)
        assert not saving.done()  # FOR SHARE가 해제의 행 잠금을 기다린다
    with pytest.raises(service.ConnectionNotActive):
        await saving
    for table in FOUR_TABLES:
        assert await ix.scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


class _GatedMail:
    """첫 `get_message`에서 멈췄다가 풀어 주면 이어간다(동기화 진행 중 상태를 만든다)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def get_message(self, message_id: str) -> object:
        self.started.set()
        await self.gate.wait()
        return await self._inner.get_message(message_id)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def test_disconnect_during_a_running_sync_leaves_zero_rows(ix: IndexEnv) -> None:
    await add_inbox(ix)
    gated = _GatedMail(ix.mail)
    syncing = asyncio.create_task(sync_connection(ix.mi, gated, ix.ws, ix.cid))  # type: ignore[arg-type]
    await asyncio.wait_for(gated.started.wait(), 5)
    await disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
    gated.gate.set()
    result = await asyncio.wait_for(syncing, 10)
    assert result.outcome == "no_connection"
    for table in FOUR_TABLES:
        assert await ix.scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


async def test_disconnect_while_a_write_is_in_flight_is_serialized(ix: IndexEnv) -> None:
    """동기화 쓰기 트랜잭션이 연결을 잠근 동안 해제는 기다렸다가 데이터를 모두 지운다."""
    await add_inbox(ix)
    async with ix.db.workspace_session(ix.ws) as writing:
        assert await store.lock_active_connection(writing, ix.ws, ix.cid)
        closing = asyncio.create_task(
            disconnect(ix.db, None, None, workspace_id=ix.ws, connection_id=ix.cid, clock=ix.clock)
        )
        await asyncio.sleep(0.3)
        assert not closing.done()
    await asyncio.wait_for(closing, 10)
    for table in FOUR_TABLES:
        assert await ix.scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


# ---------------------------------------------------------------- save_grant 교체


async def test_save_grant_replacing_a_dead_connection_purges_its_index(
    ix: IndexEnv, owner_engine: Engine
) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    assert await ix.scalar("SELECT count(*) FROM chunk") > 0
    async with ix.db.workspace_session(ix.ws) as s:
        user_id = (
            await s.execute(text("SELECT user_id FROM connection WHERE id = :id"), {"id": ix.cid})
        ).scalar_one()
        # 토큰이 거부돼 error가 된 연결: 색인은 남아 있다.
        await connection_store.mark_dead(s, ix.ws, ix.cid, status="error", now=ix.clock.now())
    assert await ix.scalar("SELECT count(*) FROM chunk") > 0

    grant = ConnectGrant(
        sub="sub-new",
        email="alice@example.com",
        scopes=DATA_SCOPES,
        refresh_token=SecretStr("synthetic-refresh-token"),
    )
    new_id = await save_grant(
        ix.db,
        None,  # type: ignore[arg-type]  # 문제가 없으면 철회를 부르지 않는다
        EnvelopeCipher(KeyEncryptionKey(bytes(range(32)))),
        workspace_id=ix.ws,
        user_id=user_id,
        grant=grant,
        clock=ix.clock,
        ids=ix.ids,
    )
    assert new_id != ix.cid
    for table in FOUR_TABLES:
        assert await ix.scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


async def test_adding_a_scope_or_requesting_sync_resets_error_state_to_pending(
    ix: IndexEnv, app_url: str
) -> None:
    ix.mi.jobs = make_procrastinate_app(app_url)
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    revoked = _Failing(PermissionRevoked("합성", reason="invalid_grant"))
    await sync_connection(ix.mi, revoked, ix.ws, ix.cid)
    stale = ix.clock.now() + timedelta(days=1)
    assert (await service.get_status(ix.mi, ix.ws, ix.cid)).status == "error"
    assert (ix.ws, ix.cid) not in await due_connections(ix, stale)  # 영구 오류라 제외

    await service.add_scope(
        ix.mi, ix.ws, ix.cid, label_id="SENT", label_name="SENT", period_days=30
    )
    status = await service.get_status(ix.mi, ix.ws, ix.cid)
    assert (status.status, status.error_kind) == ("pending", None)
    assert (ix.ws, ix.cid) in await due_connections(ix, stale)

    await sync_connection(ix.mi, revoked, ix.ws, ix.cid)
    assert (await service.get_status(ix.mi, ix.ws, ix.cid)).status == "error"
    await purge_jobs(ix.db, ix.ws)
    await service.request_sync(ix.mi, ix.ws, ix.cid)
    assert (await service.get_status(ix.mi, ix.ws, ix.cid)).status == "pending"
    await purge_jobs(ix.db, ix.ws)
