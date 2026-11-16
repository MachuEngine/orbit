"""평가용 색인 만들기·재사용 (ADR-0028 §1·§2).

색인은 운영과 같은 경로로 만든다: Sim 메일 커넥터 -> `mailindex.sync_connection`(청크·토큰화·
임베딩·저장). 지문별로 워크스페이스·연결 하나를 평가 DB(`orbit_eval`)에 두고, 지문이 같은 색인이
완성돼 있으면 재사용한다. 지문이 다른 색인은 지우지 않는다.

앱 역할(`orbit_app`)은 연결 행을 만들 수 없어서(토큰 열) 합성 사용자·연결 행만 소유자 역할로 만든다.
토큰 값은 자리 채움 바이트이고 실제 토큰이 아니다. 모든 행은 합성 데이터다.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import create_engine, text

from orbit.connectors.contracts import MailConnector
from orbit.core.clock import FixedClock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.embed_cache import CachingEmbedder
from orbit.evalrunner.retrieval.fingerprint import derived_id, index_fingerprint
from orbit.mailindex import service, store
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import Embedder
from orbit.mailindex.sync import sync_connection

_DUMMY_KEK = "eval-placeholder"


class IndexBuildError(RuntimeError):  # noqa: N818  도메인 용어
    """평가용 색인을 만들지 못했거나 기대한 메일 수와 다르다."""


@dataclass(frozen=True)
class IndexHandle:
    account_id: str
    fingerprint: str
    workspace_id: uuid.UUID
    connection_id: uuid.UUID
    reused: bool
    expected_mails: int
    indexed_mails: int
    chunks: int
    build_seconds: float
    embedded_chunks: int
    first_sync_max: int

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["workspace_id"] = str(self.workspace_id)
        out["connection_id"] = str(self.connection_id)
        return out


class EvalIndexer:
    def __init__(
        self,
        *,
        db: AsyncDatabase,
        owner_url: str,
        embedder: Embedder,
        world_sha256: str,
        expected_mails: Callable[[str], int],
        chunk_texts: Callable[[str], list[str]],
        first_sync_max: int = worldmail.EVAL_FIRST_SYNC_MAX,
    ) -> None:
        self._db = db
        self._owner_url = owner_url
        self._embedder = embedder
        self._world_sha = world_sha256
        self._expected = expected_mails
        self._chunk_texts = chunk_texts
        self._first_sync_max = first_sync_max

    def fingerprint(self, account_id: str) -> str:
        return index_fingerprint(
            world_sha256=self._world_sha,
            account_id=account_id,
            model_id=self._embedder.model_id,
            first_sync_max=self._first_sync_max,
        )

    async def ensure(self, account_id: str, mail: MailConnector, account_email: str) -> IndexHandle:
        fp = self.fingerprint(account_id)
        ws, cid = derived_id(fp, "workspace"), derived_id(fp, "connection")
        user = derived_id(fp, "user")
        clock = FixedClock(worldmail.INDEX_NOW)
        mi = MailIndexDeps(
            db=self._db,
            clock=clock,
            ids=Uuid7Generator(clock),
            embedder=self._embedder,
        )
        await self._create_rows(ws, cid, user, account_email)
        for label in worldmail.SCOPE_LABELS:
            await service.add_scope(
                mi, ws, cid, label_id=label, label_name=label,
                period_days=worldmail.SCOPE_PERIOD_DAYS,
            )  # fmt: skip
        expected = self._expected(account_id)
        indexed, chunk_rows = await self._counts(ws, cid)
        missing_emb = await self._missing_embeddings(ws, cid)
        expected_chunks = len(self._chunk_texts(account_id))
        async with self._db.workspace_session(ws) as s:
            state = await store.get_sync_state(s, ws, cid)
        complete = (
            state is not None
            and state.status == "ok"
            and state.history_id is not None
            and indexed == expected
            and chunk_rows == expected_chunks
            and missing_emb == 0
        )
        embedded = 0
        started = time.monotonic()
        if not complete:
            if isinstance(self._embedder, CachingEmbedder):
                texts = self._chunk_texts(account_id)
                if texts:
                    await self._embedder.embed(
                        texts
                    )  # 캐시를 일괄(배치)로 채운다. 동기화는 적중한다
            result = await sync_connection(mi, mail, ws, cid, first_sync_max=self._first_sync_max)
            if result.outcome != "synced":
                raise IndexBuildError(
                    f"색인 동기화 실패: account={account_id} outcome={result.outcome} "
                    f"kind={result.error_kind}"
                )
            embedded = result.embedded_chunks
        elapsed = time.monotonic() - started
        indexed, chunks = await self._counts(ws, cid)
        if indexed != expected:
            raise IndexBuildError(
                f"색인된 메일 수가 기대와 다르다: account={account_id} "
                f"indexed={indexed} expected={expected}"
            )
        return IndexHandle(
            account_id=account_id,
            fingerprint=fp,
            workspace_id=ws,
            connection_id=cid,
            reused=complete,
            expected_mails=expected,
            indexed_mails=indexed,
            chunks=chunks,
            build_seconds=0.0 if complete else round(elapsed, 3),
            embedded_chunks=embedded,
            first_sync_max=self._first_sync_max,
        )

    async def _missing_embeddings(self, ws: uuid.UUID, cid: uuid.UUID) -> int:
        """임베딩이 없거나 지금 임베딩 모델로 만들지 않은 chunk 행 수."""
        async with self._db.workspace_session(ws) as s:
            n = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM chunk WHERE workspace_id = :ws"
                        " AND connection_id = :cid"
                        " AND (embedding IS NULL OR embedding_model <> :model)"
                    ),
                    {"ws": ws, "cid": cid, "model": self._embedder.model_id},
                )
            ).scalar_one()
        return int(n)

    async def _counts(self, ws: uuid.UUID, cid: uuid.UUID) -> tuple[int, int]:
        async with self._db.workspace_session(ws) as s:
            items = await store.count_items(s, ws, cid)
            chunks = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM chunk"
                        " WHERE workspace_id = :ws AND connection_id = :cid"
                    ),
                    {"ws": ws, "cid": cid},
                )
            ).scalar_one()
        return items, int(chunks)

    async def _create_rows(
        self, ws: uuid.UUID, cid: uuid.UUID, user: uuid.UUID, account_email: str
    ) -> None:
        now = worldmail.INDEX_NOW
        async with self._db.workspace_session(ws) as s:
            await s.execute(
                text(
                    "INSERT INTO workspace (id, created_at) VALUES (:id, :now)"
                    " ON CONFLICT (id) DO NOTHING"
                ),
                {"id": ws, "now": now},
            )
        engine = create_engine(self._owner_url, pool_size=1, max_overflow=0)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws)}
                )
                conn.execute(
                    text(
                        "INSERT INTO app_user (id, google_sub, email, display_name,"
                        " created_at, last_login_at) VALUES (:id, :sub, 'eval@example.test',"
                        " 'eval', :now, :now) ON CONFLICT (id) DO NOTHING"
                    ),
                    {"id": user, "sub": f"eval-retrieval-{user}", "now": now},
                )
                conn.execute(
                    text(
                        "INSERT INTO membership (workspace_id, user_id, role)"
                        " VALUES (:w, :u, 'owner') ON CONFLICT DO NOTHING"
                    ),
                    {"w": ws, "u": user},
                )
                conn.execute(
                    text(
                        "INSERT INTO connection (id, workspace_id, user_id, provider,"
                        " account_email, account_sub, scopes, status, token_ciphertext,"
                        " token_nonce, wrapped_dek, kek_id, created_at, updated_at)"
                        " VALUES (:id, :ws, :uid, 'google', :email, 'eval', ARRAY[]::text[],"
                        " 'active', '\\x01', '\\x02', '\\x03', :kek, :now, :now)"
                        " ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id": cid, "ws": ws, "uid": user, "email": account_email,
                        "kek": _DUMMY_KEK, "now": now,
                    },
                )  # fmt: skip
        finally:
            engine.dispose()
