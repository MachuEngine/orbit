"""메일 색인 동기화 (ADR-0019 §6·§7).

- 첫 동기화: 범위별 메시지 ID 목록(최근순, 범위당 `FIRST_SYNC_MAX_MAILS`개)을 읽는다. 목록을
  읽기 전에 `current_history_id`를 잡아 두므로 읽는 동안 생긴 변경은 다음 증분 동기화가 잡는다.
- 이후: `list_history`로 증분 동기화한다. 삭제된 메시지는 지우고, 추가·라벨 변경 메시지는 다시 읽어
  범위 라벨이 있으면 색인하고 없으면 지운다. `HistoryExpired`면 전체 동기화로 돌아간다. 전체
  동기화는 목록이 기준이라 목록에 없는 기존 항목을 지운다.
- 새 범위가 생기면(첫 동기화 표시 없음) 그 범위만 목록으로 읽고 이어서 증분 동기화한다.
- 같은 연결의 동기화는 `pg_try_advisory_xact_lock`으로 직렬화한다. 이미 돌고 있으면 기다리지 않고
  `busy`로 끝난다(돌고 있는 동기화가 같은 일을 한다).
- 본문 해시와 임베딩 모델이 같으면 다시 임베딩하지 않는다.
- 로그에는 개수·ID·오류 종류만 남긴다(제목·본문·주소 금지).
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import text

from orbit.connectors.contracts import (
    ConnectorError,
    HistoryExpired,
    MailConnector,
    MailMessage,
    NotFound,
)
from orbit.connectors.errors import connector_error_kind
from orbit.mailindex import store
from orbit.mailindex.chunking import index_text, split_chunks
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import EmbeddingError
from orbit.mailindex.limits import FIRST_SYNC_MAX_MAILS
from orbit.mailindex.store import NewChunk, ScopeRow

logger = logging.getLogger(__name__)


class _ConnectionGone(Exception):
    """동기화 중에 연결이 해제됐다. 더 쓰지 않고 끝낸다."""


Outcome = Literal["synced", "busy", "no_connection", "no_scopes", "skipped", "error"]


@dataclass
class SyncResult:
    outcome: Outcome
    mode: Literal["full", "incremental"] | None = None
    fetched: int = 0
    indexed: int = 0
    deleted: int = 0
    embedded_chunks: int = 0
    error_kind: str | None = None


def _lock_key(connection_id: uuid.UUID) -> str:
    return f"mail_index_sync:{connection_id}"


def _in_scope(message: MailMessage, scopes: list[ScopeRow], now: datetime) -> bool:
    """메시지에 범위 라벨이 하나라도 있고 그 범위의 기간 안이면 색인 대상이다."""
    return any(
        s.label_id in message.labels and message.sent_at >= now - timedelta(days=s.period_days)
        for s in scopes
    )


async def sync_connection(
    mi: MailIndexDeps,
    mail: MailConnector,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    incremental_only: bool = False,
) -> SyncResult:
    """연결 하나를 동기화한다. 연결이 없거나 해제됐으면 아무것도 하지 않는다.

    `incremental_only`(질의 직전 동기화)이면 첫 동기화·새 범위 읽기·전체 동기화는 하지 않고
    `skipped`로 끝낸다. 커넥터·임베딩 오류는 상태에 `error`로 기록하고 `error` 결과로 돌려준다.
    """
    # 연결 사용: 이 잠금 트랜잭션 하나와, 메시지마다 여는 짧은 세션 하나로 최대 둘이다. 풀은
    # 프로세스당 5+10(`AsyncDatabase.from_url` 기본)이다. 워커 프로세스는 큐마다 동시성 1인 워커를
    # 따로 돌리므로(run 큐 1, 색인 큐 1) 동시에 쓰는 연결은 색인 2 + run 1 + 질의 직전 동기화
    # 2(run 안에서 도는 경우) 정도로 최대 5개 안팎이다. 이 구조에서는 고갈되지 않는다. 큐 동시성을
    # 올리면 연결 수가 큐 동시성 x 2만큼 늘어나므로 풀 크기를 함께 올린다.
    async with mi.db.workspace_session(workspace_id) as lock_session:
        locked = (
            await lock_session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": _lock_key(connection_id)},
            )
        ).scalar_one()
        if not locked:
            return SyncResult("busy")
        return await _locked(mi, mail, workspace_id, connection_id, incremental_only)


async def _locked(
    mi: MailIndexDeps,
    mail: MailConnector,
    ws: uuid.UUID,
    cid: uuid.UUID,
    incremental_only: bool,
) -> SyncResult:
    async with mi.db.workspace_session(ws) as s:
        status = await store.connection_status(s, ws, cid)
        scopes = await store.list_scopes(s, ws, cid) if status == "active" else []
        state = await store.get_sync_state(s, ws, cid) if scopes else None
    if status != "active":
        return SyncResult("no_connection")
    if not scopes:
        return SyncResult("no_scopes")

    run = _Run(mi, mail, ws, cid, scopes)
    try:
        return await _drive(mi, mail, ws, cid, run, scopes, state, incremental_only)
    except _ConnectionGone:
        logger.info("메일 색인 동기화 중 연결이 해제됐다: connection_id=%s", cid)
        run.result.outcome = "no_connection"
        return run.result


async def _drive(
    mi: MailIndexDeps,
    mail: MailConnector,
    ws: uuid.UUID,
    cid: uuid.UUID,
    run: "_Run",
    scopes: list[ScopeRow],
    state: store.SyncStateRow | None,
    incremental_only: bool,
) -> SyncResult:
    try:
        if state is None or state.history_id is None:
            if incremental_only:
                return SyncResult("skipped")
            run.result.mode = "full"
            history_id = await mail.current_history_id()
            await run.full(scopes, authoritative=True)
            synced_scopes = scopes
        else:
            pending = [s for s in scopes if s.initial_synced_at is None]
            synced_scopes = []
            if pending and not incremental_only:
                await run.full(pending, authoritative=False)
                synced_scopes = pending
            run.result.mode = run.result.mode or "incremental"
            try:
                history_id = await run.incremental(state.history_id)
            except HistoryExpired:
                if incremental_only:
                    return SyncResult("skipped")
                run.result.mode = "full"
                history_id = await mail.current_history_id()
                await run.full(scopes, authoritative=True)
                synced_scopes = scopes
    except (ConnectorError, EmbeddingError) as exc:
        kind = (
            f"embedding_{exc.kind}"
            if isinstance(exc, EmbeddingError)
            else connector_error_kind(exc)
        )
        async with mi.db.workspace_session(ws) as s:
            if not await store.lock_active_connection(s, ws, cid):
                raise _ConnectionGone from None
            await store.save_sync_state(
                s, ws=ws, cid=cid, status="error", now=mi.clock.now(), error_kind=kind
            )
        logger.warning("메일 색인 동기화 실패: connection_id=%s kind=%s", cid, kind)
        run.result.outcome = "error"
        run.result.error_kind = kind
        return run.result

    now = mi.clock.now()
    async with mi.db.workspace_session(ws) as s:
        if not await store.lock_active_connection(s, ws, cid):
            raise _ConnectionGone
        run.result.deleted += await store.prune_out_of_scope(s, ws, cid, now=now)  # 보존 기간
        if synced_scopes:
            await store.mark_scopes_synced(s, ws, cid, [x.id for x in synced_scopes], now)
        await store.save_sync_state(
            s, ws=ws, cid=cid, status="ok", now=now, history_id=history_id, synced_at=now
        )
    logger.info(
        "메일 색인 동기화: connection_id=%s mode=%s fetched=%d indexed=%d deleted=%d chunks=%d",
        cid,
        run.result.mode,
        run.result.fetched,
        run.result.indexed,
        run.result.deleted,
        run.result.embedded_chunks,
    )
    run.result.outcome = "synced"
    return run.result


class _Run:
    """동기화 한 번의 진행 상태. 메시지마다 읽고 → 임베딩하고 → 짧은 트랜잭션으로 저장한다."""

    def __init__(
        self,
        mi: MailIndexDeps,
        mail: MailConnector,
        ws: uuid.UUID,
        cid: uuid.UUID,
        scopes: list[ScopeRow],
    ) -> None:
        self._mi = mi
        self._mail = mail
        self._ws = ws
        self._cid = cid
        self._scopes = scopes
        self.result = SyncResult("synced")

    # ---- 전체(목록 기준) ----

    async def full(self, scopes: list[ScopeRow], *, authoritative: bool) -> None:
        now = self._mi.clock.now()
        ids: dict[str, None] = {}
        for scope in scopes:
            after = now - timedelta(days=scope.period_days)
            listed: list[str] = []
            token: str | None = None
            while len(listed) < FIRST_SYNC_MAX_MAILS:
                page = await self._mail.list_message_ids(
                    scope.label_id, after=after, page_token=token
                )
                listed += page.message_ids
                token = page.next_page_token
                if token is None:
                    break
            ids.update(dict.fromkeys(listed[:FIRST_SYNC_MAX_MAILS]))
        kept: list[str] = []
        for message_id in ids:
            if await self._fetch_and_index(message_id):
                kept.append(message_id)
        if authoritative:
            async with self._mi.db.workspace_session(self._ws) as s:
                self.result.deleted += await store.delete_items_not_in(s, self._ws, self._cid, kept)

    # ---- 증분 ----

    async def incremental(self, start_history_id: str) -> str:
        changed: dict[str, None] = {}
        deleted: dict[str, None] = {}
        token: str | None = None
        history_id = start_history_id
        while True:
            page = await self._mail.list_history(start_history_id, page_token=token)
            changed.update(dict.fromkeys(page.added_ids))
            changed.update(dict.fromkeys(page.labels_added_ids))
            changed.update(dict.fromkeys(page.labels_removed_ids))
            deleted.update(dict.fromkeys(page.deleted_ids))
            history_id = page.history_id
            token = page.next_page_token
            if token is None:
                break
        async with self._mi.db.workspace_session(self._ws) as s:
            self.result.deleted += await store.delete_items(s, self._ws, self._cid, list(deleted))
        for message_id in changed:
            if message_id not in deleted:
                await self._fetch_and_index(message_id)
        return history_id

    # ---- 메시지 하나 ----

    async def _delete(self, message_id: str) -> None:
        async with self._mi.db.workspace_session(self._ws) as s:
            self.result.deleted += await store.delete_items(s, self._ws, self._cid, [message_id])

    async def _fetch_and_index(self, message_id: str) -> bool:
        """메시지를 읽어 색인한다. 색인 대상이 아니거나 없으면 기존 항목을 지우고 False."""
        mi = self._mi
        try:
            message = await self._mail.get_message(message_id)
        except NotFound:
            await self._delete(message_id)
            return False
        self.result.fetched += 1
        if not _in_scope(message, self._scopes, mi.clock.now()) or not index_text(
            message.subject, message.body_text
        ):
            await self._delete(message_id)
            return False
        chunk_texts = split_chunks(message.subject, message.body_text)
        digest = hashlib.sha256(
            index_text(message.subject, message.body_text).encode("utf-8")
        ).hexdigest()
        now = mi.clock.now()
        model_id = mi.embedder.model_id

        async with mi.db.workspace_session(self._ws) as s:
            existing = await store.find_item(s, self._ws, self._cid, message_id, model_id)
        fresh = (
            existing is not None
            and existing.body_hash == digest
            and existing.fresh_chunks == len(chunk_texts)
        )
        new_chunks: list[NewChunk] = []
        if not fresh:
            vectors = await mi.embedder.embed(chunk_texts)
            new_chunks = [
                NewChunk(
                    ord=i,
                    content=content,
                    token_text=" ".join(mi.tokenize(content)),
                    embedding=vector,
                )
                for i, (content, vector) in enumerate(zip(chunk_texts, vectors, strict=True))
            ]
        async with mi.db.workspace_session(self._ws) as s:
            if not await store.lock_active_connection(s, self._ws, self._cid):
                raise _ConnectionGone
            current = await store.list_scopes(s, self._ws, self._cid, share_lock=True)
            if not _in_scope(message, current, now):
                # 읽는 사이 범위가 지워졌다. 쓰지 않고 기존 항목만 정리한다.
                await store.delete_items(s, self._ws, self._cid, [message_id])
                return False
            item_id = await store.upsert_item(
                s,
                ws=self._ws,
                cid=self._cid,
                item_id=mi.ids.next(),
                provider_item_id=message.id,
                thread_id=message.thread_id,
                subject=message.subject,
                sender_name=message.from_.name,
                sender_email=message.from_.email,
                sent_at=message.sent_at,
                label_ids=list(message.labels),
                body_hash=digest,
                now=now,
            )
            if not fresh:
                await store.replace_chunks(
                    s,
                    ws=self._ws,
                    cid=self._cid,
                    item_id=item_id,
                    chunks=new_chunks,
                    chunk_ids=[mi.ids.next() for _ in new_chunks],
                    model_id=model_id,
                    now=now,
                )
                self.result.embedded_chunks += len(new_chunks)
        self.result.indexed += 1
        return True


async def sync_before_query(
    mi: MailIndexDeps,
    mail: MailConnector,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    last_synced_at: datetime | None,
) -> bool:
    """질의 직전 동기화(ADR-0019 §6). 마지막 동기화가 `query_stale`보다 오래됐으면 시간 상한 안에서
    증분 동기화를 한다. 실패·초과면 아무것도 바꾸지 않은 것처럼 돌아간다(기존 색인으로 검색).

    동기화를 시도했으면 True. 어떤 예외도 호출자에게 던지지 않는다(원인은 종류만 로그에 남긴다).
    """
    if last_synced_at is None:
        return False  # 첫 동기화 전이다. 질의 경로에서 처음부터 읽지 않는다
    if mi.clock.now() - last_synced_at <= mi.query_stale:
        return False
    try:
        await asyncio.wait_for(
            sync_connection(mi, mail, workspace_id, connection_id, incremental_only=True),
            timeout=mi.query_sync_timeout_s,
        )
    except TimeoutError:
        logger.warning("질의 직전 동기화 시간 초과: connection_id=%s", connection_id)
    except Exception as exc:  # noqa: BLE001 - 검색을 막지 않는다. 종류만 남긴다
        logger.warning(
            "질의 직전 동기화 실패: connection_id=%s kind=%s", connection_id, type(exc).__name__
        )
    return True
