"""색인 테이블 접근 (마이그레이션 0008). 모든 함수는 워크스페이스 세션을 받고 `workspace_id`와
`connection_id`로 거른다. 제목·본문·주소는 로그에 남기지 않는다."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.mailindex.limits import EMBEDDING_DIM

KIND_MAIL = "mail"


@dataclass(frozen=True)
class ScopeRow:
    id: uuid.UUID
    label_id: str
    label_name: str
    period_days: int
    initial_synced_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class SyncStateRow:
    history_id: str | None
    last_synced_at: datetime | None
    status: str
    error_kind: str | None


@dataclass(frozen=True)
class ItemRow:
    id: uuid.UUID
    body_hash: str
    fresh_chunks: int  # 현재 임베딩 모델로 만든 청크 수


@dataclass(frozen=True)
class NewChunk:
    ord: int
    content: str
    token_text: str  # 공백으로 이은 형태소 토큰(`simple` tsvector의 입력)
    embedding: list[float]


def vector_literal(vector: list[float]) -> str:
    """pgvector 텍스트 표현(`[0.1,0.2,...]`). 파이썬 패키지 없이 `CAST(... AS vector)`로 넘긴다."""
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(f"임베딩 차원은 {EMBEDDING_DIM}이어야 한다")
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


# ---------------------------------------------------------------- 연결·범위·상태


async def connection_status(session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID) -> str | None:
    value = (
        await session.execute(
            text("SELECT status FROM connection WHERE id = :cid AND workspace_id = :ws"),
            {"cid": cid, "ws": ws},
        )
    ).scalar_one_or_none()
    return None if value is None else str(value)


async def lock_active_connection(session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID) -> bool:
    """연결이 활성이면 행을 `FOR SHARE`로 잠그고 True. 쓰기 직전에 부른다.

    연결 해제(`UPDATE connection`)는 이 잠금이 풀릴 때까지 기다린 뒤 색인 데이터를 지우므로, 해제와
    겹친 동기화가 해제 뒤에 데이터를 남기지 못한다.
    """
    found = (
        await session.execute(
            text(
                "SELECT 1 FROM connection WHERE id = :cid AND workspace_id = :ws"
                " AND status = 'active' FOR SHARE"
            ),
            {"cid": cid, "ws": ws},
        )
    ).first()
    return found is not None


async def find_active_connection(session: AsyncSession, ws: uuid.UUID) -> uuid.UUID | None:
    value = (
        await session.execute(
            text(
                "SELECT id FROM connection WHERE workspace_id = :ws AND status = 'active'"
                " ORDER BY created_at, id LIMIT 1"
            ),
            {"ws": ws},
        )
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def _scope(r: object) -> ScopeRow:
    return ScopeRow(
        id=r.id,  # type: ignore[attr-defined]
        label_id=r.label_id,  # type: ignore[attr-defined]
        label_name=r.label_name,  # type: ignore[attr-defined]
        period_days=r.period_days,  # type: ignore[attr-defined]
        initial_synced_at=r.initial_synced_at,  # type: ignore[attr-defined]
        created_at=r.created_at,  # type: ignore[attr-defined]
    )


async def list_scopes(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID, *, share_lock: bool = False
) -> list[ScopeRow]:
    """범위 목록. `share_lock`이면 행을 `FOR SHARE`로 잠가 겹친 범위 삭제가 끝난 뒤 쓰게 한다."""
    lock = " FOR SHARE" if share_lock else ""
    rows = await session.execute(
        text(
            "SELECT id, label_id, label_name, period_days, initial_synced_at, created_at"  # noqa: S608
            " FROM index_scope WHERE workspace_id = :ws AND connection_id = :cid"
            f" ORDER BY created_at, id{lock}"
        ),
        {"ws": ws, "cid": cid},
    )
    return [_scope(r) for r in rows]


async def upsert_scope(
    session: AsyncSession,
    *,
    ws: uuid.UUID,
    cid: uuid.UUID,
    scope_id: uuid.UUID,
    label_id: str,
    label_name: str,
    period_days: int,
    now: datetime,
) -> ScopeRow:
    """같은 라벨이 있으면 이름·기간을 바꾼다. 기간이 늘면 다시 읽도록 첫 동기화 표시를 지운다."""
    row = (
        await session.execute(
            text(
                "INSERT INTO index_scope (id, workspace_id, connection_id, label_id, label_name,"
                " period_days, created_at) VALUES (:id, :ws, :cid, :label, :name, :days, :now)"
                " ON CONFLICT (connection_id, label_id) DO UPDATE SET"
                " label_name = EXCLUDED.label_name, period_days = EXCLUDED.period_days,"
                " initial_synced_at = CASE WHEN EXCLUDED.period_days > index_scope.period_days"
                " THEN NULL ELSE index_scope.initial_synced_at END"
                " RETURNING id, label_id, label_name, period_days, initial_synced_at, created_at"
            ),
            {
                "id": scope_id,
                "ws": ws,
                "cid": cid,
                "label": label_id,
                "name": label_name,
                "days": period_days,
                "now": now,
            },
        )
    ).one()
    return _scope(row)


async def delete_scope(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID, scope_id: uuid.UUID
) -> bool:
    result = await session.execute(
        text(
            "DELETE FROM index_scope WHERE id = :id AND workspace_id = :ws AND connection_id = :cid"
        ),
        {"id": scope_id, "ws": ws, "cid": cid},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def mark_scopes_synced(
    session: AsyncSession,
    ws: uuid.UUID,
    cid: uuid.UUID,
    scope_ids: list[uuid.UUID],
    now: datetime,
) -> None:
    await session.execute(
        text(
            "UPDATE index_scope SET initial_synced_at = :now WHERE workspace_id = :ws"
            " AND connection_id = :cid AND id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"now": now, "ws": ws, "cid": cid, "ids": scope_ids},
    )


async def get_sync_state(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID
) -> SyncStateRow | None:
    r = (
        await session.execute(
            text(
                "SELECT history_id, last_synced_at, status, error_kind FROM sync_state"
                " WHERE workspace_id = :ws AND connection_id = :cid"
            ),
            {"ws": ws, "cid": cid},
        )
    ).one_or_none()
    if r is None:
        return None
    return SyncStateRow(r.history_id, r.last_synced_at, str(r.status), r.error_kind)


async def ensure_sync_state(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID, now: datetime
) -> None:
    """상태 행이 없으면 `pending`으로 만든다. 있으면 그대로 둔다."""
    await session.execute(
        text(
            "INSERT INTO sync_state (connection_id, workspace_id, status, updated_at)"
            " VALUES (:cid, :ws, 'pending', :now) ON CONFLICT (connection_id) DO NOTHING"
        ),
        {"cid": cid, "ws": ws, "now": now},
    )


async def reset_error_state(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID, now: datetime
) -> None:
    """`error` 상태를 `pending`으로 되돌려 주기 대상에 다시 들어오게 한다(영구 오류 포함)."""
    await session.execute(
        text(
            "UPDATE sync_state SET status = 'pending', error_kind = NULL, updated_at = :now"
            " WHERE workspace_id = :ws AND connection_id = :cid AND status = 'error'"
        ),
        {"ws": ws, "cid": cid, "now": now},
    )


async def save_sync_state(
    session: AsyncSession,
    *,
    ws: uuid.UUID,
    cid: uuid.UUID,
    status: str,
    now: datetime,
    history_id: str | None = None,
    synced_at: datetime | None = None,
    error_kind: str | None = None,
) -> None:
    """상태를 저장한다. `history_id`·`synced_at`이 None이면 기존 값을 유지한다."""
    await session.execute(
        text(
            "INSERT INTO sync_state (connection_id, workspace_id, history_id, last_synced_at,"
            " status, error_kind, updated_at)"
            " VALUES (:cid, :ws, :hid, :synced, :status, :err, :now)"
            " ON CONFLICT (connection_id) DO UPDATE SET"
            " history_id = COALESCE(EXCLUDED.history_id, sync_state.history_id),"
            " last_synced_at = COALESCE(EXCLUDED.last_synced_at, sync_state.last_synced_at),"
            " status = EXCLUDED.status, error_kind = EXCLUDED.error_kind,"
            " updated_at = EXCLUDED.updated_at"
        ),
        {
            "cid": cid,
            "ws": ws,
            "hid": history_id,
            "synced": synced_at,
            "status": status,
            "err": error_kind,
            "now": now,
        },
    )


async def delete_sync_state(session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID) -> None:
    await session.execute(
        text("DELETE FROM sync_state WHERE workspace_id = :ws AND connection_id = :cid"),
        {"ws": ws, "cid": cid},
    )


# ---------------------------------------------------------------- 항목·청크


async def find_item(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID, provider_item_id: str, model_id: str
) -> ItemRow | None:
    r = (
        await session.execute(
            text(
                "SELECT si.id, si.body_hash, (SELECT count(*) FROM chunk c"
                " WHERE c.source_item_id = si.id AND c.workspace_id = :ws"
                " AND c.embedding_model = :model) AS fresh"
                " FROM source_item si WHERE si.workspace_id = :ws AND si.connection_id = :cid"
                " AND si.provider_item_id = :pid"
            ),
            {"ws": ws, "cid": cid, "pid": provider_item_id, "model": model_id},
        )
    ).one_or_none()
    return None if r is None else ItemRow(r.id, r.body_hash, int(r.fresh))


async def upsert_item(
    session: AsyncSession,
    *,
    ws: uuid.UUID,
    cid: uuid.UUID,
    item_id: uuid.UUID,
    provider_item_id: str,
    thread_id: str,
    subject: str,
    sender_name: str | None,
    sender_email: str,
    sent_at: datetime,
    label_ids: list[str],
    body_hash: str,
    now: datetime,
) -> uuid.UUID:
    """항목을 만들거나 갱신하고 ID를 돌려준다. 같은 연결·제공자 ID는 같은 행이다."""
    value = (
        await session.execute(
            text(
                "INSERT INTO source_item (id, workspace_id, connection_id, kind, provider_item_id,"
                " thread_id, subject, sender_name, sender_email, sent_at, label_ids, body_hash,"
                " indexed_at) VALUES (:id, :ws, :cid, :kind, :pid, :thread, :subject, :sname,"
                " :semail, :sent, :labels, :hash, :now)"
                " ON CONFLICT (connection_id, provider_item_id) DO UPDATE SET"
                " thread_id = EXCLUDED.thread_id, subject = EXCLUDED.subject,"
                " sender_name = EXCLUDED.sender_name, sender_email = EXCLUDED.sender_email,"
                " sent_at = EXCLUDED.sent_at, label_ids = EXCLUDED.label_ids,"
                " body_hash = EXCLUDED.body_hash, indexed_at = EXCLUDED.indexed_at"
                " RETURNING id"
            ),
            {
                "id": item_id,
                "ws": ws,
                "cid": cid,
                "kind": KIND_MAIL,
                "pid": provider_item_id,
                "thread": thread_id,
                "subject": subject,
                "sname": sender_name,
                "semail": sender_email,
                "sent": sent_at,
                "labels": label_ids,
                "hash": body_hash,
                "now": now,
            },
        )
    ).scalar_one()
    return uuid.UUID(str(value))


async def replace_chunks(
    session: AsyncSession,
    *,
    ws: uuid.UUID,
    cid: uuid.UUID,
    item_id: uuid.UUID,
    chunks: list[NewChunk],
    chunk_ids: list[uuid.UUID],
    model_id: str,
    now: datetime,
) -> None:
    await session.execute(
        text("DELETE FROM chunk WHERE source_item_id = :item AND workspace_id = :ws"),
        {"item": item_id, "ws": ws},
    )
    for chunk, chunk_id in zip(chunks, chunk_ids, strict=True):
        await session.execute(
            text(
                "INSERT INTO chunk (id, workspace_id, connection_id, source_item_id, ord, content,"
                " tokens, embedding, embedding_model, created_at) VALUES (:id, :ws, :cid, :item,"
                " :ord, :content, to_tsvector('simple', :tokens), CAST(:emb AS vector), :model,"
                " :now)"
            ),
            {
                "id": chunk_id,
                "ws": ws,
                "cid": cid,
                "item": item_id,
                "ord": chunk.ord,
                "content": chunk.content,
                "tokens": chunk.token_text,
                "emb": vector_literal(chunk.embedding),
                "model": model_id,
                "now": now,
            },
        )


async def delete_items(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID, provider_item_ids: list[str]
) -> int:
    """제공자 메시지 ID로 항목을 지운다(청크는 CASCADE). 지운 개수를 돌려준다."""
    if not provider_item_ids:
        return 0
    result = await session.execute(
        text(
            "DELETE FROM source_item WHERE workspace_id = :ws AND connection_id = :cid"
            " AND provider_item_id = ANY(CAST(:ids AS text[]))"
        ),
        {"ws": ws, "cid": cid, "ids": provider_item_ids},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def delete_items_not_in(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID, keep_provider_item_ids: list[str]
) -> int:
    """`keep` 밖의 항목을 모두 지운다(전체 동기화가 목록의 기준이 될 때)."""
    result = await session.execute(
        text(
            "DELETE FROM source_item WHERE workspace_id = :ws AND connection_id = :cid"
            " AND NOT (provider_item_id = ANY(CAST(:keep AS text[])))"
        ),
        {"ws": ws, "cid": cid, "keep": keep_provider_item_ids},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def prune_out_of_scope(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID, *, now: datetime | None = None
) -> int:
    """어느 남은 범위 라벨에도 속하지 않는 항목을 지운다(청크는 CASCADE).

    `now`를 주면 보존 기간도 적용한다(ADR-0019 §7): 항목이 속한 범위들 가운데 어느 기간 안에도
    들지 않으면, 즉 가장 긴 기간 밖으로 밀렸으면 지운다.
    """
    period = (
        " AND si.sent_at >= CAST(:now AS timestamptz) - s.period_days * interval '1 day'"
        if now is not None
        else ""
    )
    result = await session.execute(
        text(
            "DELETE FROM source_item si WHERE si.workspace_id = :ws AND si.connection_id = :cid"  # noqa: S608
            " AND NOT EXISTS (SELECT 1 FROM index_scope s WHERE s.workspace_id = si.workspace_id"
            f" AND s.connection_id = si.connection_id AND s.label_id = ANY(si.label_ids){period})"
        ),
        {"ws": ws, "cid": cid} | ({"now": now} if now is not None else {}),
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def has_pending_sync_job(session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID) -> bool:
    """이 연결의 동기화 작업이 대기(`todo`) 또는 실행(`doing`) 중인지. 작업 인자의 ID로만 찾는다."""
    found = (
        await session.execute(
            text(
                "SELECT 1 FROM procrastinate_jobs WHERE task_name = 'sync_mail_index'"
                " AND args->>'workspace_id' = :ws AND args->>'connection_id' = :cid"
                " AND status IN ('todo', 'doing') LIMIT 1"
            ),
            {"ws": str(ws), "cid": str(cid)},
        )
    ).first()
    return found is not None


async def count_items(session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM source_item WHERE workspace_id = :ws"
                    " AND connection_id = :cid"
                ),
                {"ws": ws, "cid": cid},
            )
        ).scalar_one()
    )


async def purge_connection(session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID) -> None:
    """연결의 색인 데이터를 모두 지운다(ADR-0019 §7). 호출자의 트랜잭션 안에서 쓴다."""
    params = {"ws": ws, "cid": cid}
    for table in ("chunk", "source_item", "sync_state", "index_scope"):
        await session.execute(
            text(f"DELETE FROM {table} WHERE workspace_id = :ws AND connection_id = :cid"),  # noqa: S608
            params,
        )
