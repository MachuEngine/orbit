"""색인 범위 관리와 동기화 요청, 상태 조회 (ADR-0019 §1·§6·§7). API는 이 함수만 부른다.

범위 저장·삭제와 동기화 등록은 각자 한 트랜잭션이다. 범위를 지우면 남은 범위 어디에도 속하지 않는
항목과 청크를 같은 트랜잭션에서 지운다(소프트 삭제 없음).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from orbit.mailindex import store
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.jobs import defer_sync
from orbit.mailindex.limits import PERIOD_DAYS
from orbit.mailindex.store import ScopeRow

MAX_LABEL_NAME_CHARS = 200


class ConnectionNotActive(Exception):
    """연결이 없거나 활성 상태가 아니다. 메시지에는 ID만 담는다."""


class InvalidScope(ValueError):
    """범위 입력이 맞지 않는다(기간은 30·90·365일, 라벨 ID는 비어 있으면 안 된다)."""


@dataclass(frozen=True)
class IndexStatus:
    scopes: tuple[ScopeRow, ...]
    indexed_mail_count: int
    last_synced_at: datetime | None
    status: str  # none(범위 없음) | pending | ok | error
    error_kind: str | None
    sync_queued: bool = False  # 동기화 작업이 대기 또는 실행 중이다


async def add_scope(
    mi: MailIndexDeps,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    label_id: str,
    label_name: str,
    period_days: int,
) -> ScopeRow:
    """범위를 저장하고 동기화 작업을 등록한다. 같은 라벨이 있으면 기간·이름을 갱신한다."""
    if period_days not in PERIOD_DAYS:
        raise InvalidScope("period_days")
    if not label_id.strip():
        raise InvalidScope("label_id")
    now = mi.clock.now()
    async with mi.db.workspace_session(workspace_id) as s:
        # FOR SHARE: 연결 해제(UPDATE connection)와 직렬화된다. 해제가 먼저면 여기서 거절되고,
        # 이쪽이 먼저면 해제가 끝나면서 이 범위와 동기화 상태까지 함께 지운다.
        if not await store.lock_active_connection(s, workspace_id, connection_id):
            raise ConnectionNotActive(f"connection_id={connection_id}")
        scope = await store.upsert_scope(
            s,
            ws=workspace_id,
            cid=connection_id,
            scope_id=mi.ids.next(),
            label_id=label_id.strip(),
            label_name=label_name.strip()[:MAX_LABEL_NAME_CHARS] or label_id.strip(),
            period_days=period_days,
            now=now,
        )
        await store.ensure_sync_state(s, workspace_id, connection_id, now)
        await store.reset_error_state(s, workspace_id, connection_id, now)  # 사용자가 다시 시도했다
        if mi.jobs is not None:
            await defer_sync(s, mi.jobs, workspace_id=workspace_id, connection_id=connection_id)
    return scope


async def remove_scope(
    mi: MailIndexDeps, workspace_id: uuid.UUID, connection_id: uuid.UUID, scope_id: uuid.UUID
) -> bool:
    """범위를 지우고 남은 범위 밖 항목을 같은 트랜잭션에서 지운다. 범위가 없었으면 False."""
    async with mi.db.workspace_session(workspace_id) as s:
        if not await store.delete_scope(s, workspace_id, connection_id, scope_id):
            return False
        await store.prune_out_of_scope(s, workspace_id, connection_id, now=mi.clock.now())
        if not await store.list_scopes(s, workspace_id, connection_id):
            await store.delete_sync_state(s, workspace_id, connection_id)  # 다시 추가하면 처음부터
    return True


async def request_sync(
    mi: MailIndexDeps, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> bool:
    """'지금 동기화'. 작업을 새로 등록했으면 True, 범위가 없거나 이미 대기 중이면 False."""
    async with mi.db.workspace_session(workspace_id) as s:
        if not await store.lock_active_connection(s, workspace_id, connection_id):
            raise ConnectionNotActive(f"connection_id={connection_id}")
        if mi.jobs is None or not await store.list_scopes(s, workspace_id, connection_id):
            return False
        await store.reset_error_state(s, workspace_id, connection_id, mi.clock.now())
        job_id = await defer_sync(
            s, mi.jobs, workspace_id=workspace_id, connection_id=connection_id
        )
    return job_id is not None


async def get_status(
    mi: MailIndexDeps, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> IndexStatus:
    """범위 목록, 색인 메일 수, 마지막 동기화 시각, 상태."""
    async with mi.db.workspace_session(workspace_id) as s:
        scopes = await store.list_scopes(s, workspace_id, connection_id)
        state = await store.get_sync_state(s, workspace_id, connection_id)
        count = await store.count_items(s, workspace_id, connection_id)
        queued = await store.has_pending_sync_job(s, workspace_id, connection_id)
    status = (state.status if state is not None else "pending") if scopes else "none"
    return IndexStatus(
        scopes=tuple(scopes),
        indexed_mail_count=count,
        last_synced_at=None if state is None else state.last_synced_at,
        status=status,
        error_kind=None if state is None else state.error_kind,
        sync_queued=queued,
    )
