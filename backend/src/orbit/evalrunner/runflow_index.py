"""`runflow_v0`의 색인 검색 켜기(`--index on`): 계정별 평가용 색인과 에피소드 연결.

근거: ADR-0028 §1·§2, ADR-0023 §8.

- 색인은 실험 시작 때 과제에 나오는 계정마다 ADR-0028 인덱서(운영 경로 `mailindex` 동기화)로
  만들거나 재사용한다. 지문(세계 해시·계정·청크·토큰화·임베딩 모델·`INDEX_CODE_VERSION`)이
  같으면 재사용하고 임베딩 캐시도 같이 쓴다. 색인은 지문별 워크스페이스 하나에 있고
  에피소드 워크스페이스와 다르다.
- 에피소드의 `search_indexed_mail`은 `PinnedWorkspaceDatabase`를 거쳐 그 계정 색인 워크스페이스만
  읽는다. 도구가 요청한 워크스페이스는 에피소드 워크스페이스뿐이어야 하고(아니면 오류), 세션과
  쿼리의 워크스페이스 인자가 색인 워크스페이스 하나로 고정된다. 운영 코드(`runflow`)는 바꾸지
  않는다. 다른 계정·다른 지문 색인은 읽을 경로가 없다.
- 고정 시점 스냅샷이다. 질의 직전 동기화(`sync_before_query`)는 끈다(`query_stale`을 사실상 무한으로
  둔다). `indexed_as_of`는 색인을 만든 고정 시각(`worldmail.INDEX_NOW`)이다.
- 한계: 에피소드 중 Sim 쓰기(발송·일정 변경 등)는 색인에 반영하지 않는다. 색인은 에피소드 전 세계의
  스냅샷이고, 과제 기준 시각(`as_of`)이 색인 시각보다 이르면 그 사이의 메일도 색인에 있다.
"""

import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import Clock, FixedClock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.embed_cache import CachingEmbedder
from orbit.evalrunner.retrieval.indexer import EvalIndexer, IndexHandle
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import Embedder

# 질의 직전 동기화를 끄려고 "마지막 동기화가 이보다 오래됐을 때만 동기화한다"의 기준을 크게 둔다.
NEVER_STALE = timedelta(days=36_500)


class IndexNotBuilt(RuntimeError):  # noqa: N818  도메인 용어
    """과제 계정의 평가용 색인이 만들어지지 않았다."""


class WrongWorkspace(RuntimeError):  # noqa: N818  도메인 용어
    """색인 검색이 에피소드 워크스페이스가 아닌 다른 워크스페이스를 요청했다."""


class _RemappedSession:
    """`execute`의 바인드 인자 중 에피소드 워크스페이스 ID를 색인 워크스페이스 ID로 바꿔 넘긴다.

    색인 저장소 함수(`mailindex.store`·`search`)는 `workspace_id = :ws` 조건에 호출자가 준 ID를
    쓴다.
    그래서 에피소드 워크스페이스 ID를 받은 채로 색인 행을 읽으려면 인자를 바꿔야 한다. 세션 자체도
    색인 워크스페이스로 열려 있어(RLS) 바꾸지 않은 조건으로는 어떤 행도 읽히지 않는다.
    """

    def __init__(self, inner: AsyncSession, episode_ws: uuid.UUID, index_ws: uuid.UUID) -> None:
        self._inner = inner
        self._episode_ws = episode_ws
        self._index_ws = index_ws

    async def execute(
        self, statement: Any, params: Mapping[str, Any] | None = None, **kw: Any
    ) -> Any:
        if params is not None:
            params = {k: self._index_ws if v == self._episode_ws else v for k, v in params.items()}
        return await self._inner.execute(statement, params, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class PinnedWorkspaceDatabase(AsyncDatabase):
    """에피소드 워크스페이스 요청을 색인 워크스페이스 하나로 연결하는 `AsyncDatabase`.

    `workspace_session`만 지원한다. 요청한 워크스페이스가 에피소드 워크스페이스가 아니면
    `WrongWorkspace`다. 실제로 여는 세션은 항상 색인 워크스페이스의 것이고, 쿼리의 워크스페이스
    인자는 색인 워크스페이스로 바뀐다(`_RemappedSession`).
    """

    def __init__(
        self, inner: AsyncDatabase, *, episode_workspace: uuid.UUID, index_workspace: uuid.UUID
    ) -> None:  # 부모 생성자는 부르지 않는다(엔진을 새로 만들지 않고 `inner`에 위임한다)
        self._inner = inner
        self._episode_ws = episode_workspace
        self._index_ws = index_workspace

    @asynccontextmanager
    async def workspace_session(self, workspace_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        if workspace_id != self._episode_ws:
            raise WrongWorkspace("색인 검색이 에피소드 밖의 워크스페이스를 요청했다")
        async with self._inner.workspace_session(self._index_ws) as session:
            yield cast(AsyncSession, _RemappedSession(session, self._episode_ws, self._index_ws))


class EvalIndexProvider:
    """과제 계정 -> 평가용 색인 핸들. 에피소드마다 그 계정 색인에 고정된 의존성을 만든다."""

    def __init__(self, handles: Mapping[str, IndexHandle], embedder: Embedder) -> None:
        self._handles = dict(handles)
        self._embedder = embedder

    @property
    def handles(self) -> Mapping[str, IndexHandle]:
        return self._handles

    def mail_index_for(
        self,
        *,
        account_id: str,
        db: AsyncDatabase,
        clock: Clock,
        ids: Uuid7Generator,
        episode_workspace: uuid.UUID,
    ) -> MailIndexDeps:
        handle = self._handles.get(account_id)
        if handle is None:
            raise IndexNotBuilt(f"색인이 없는 계정: {account_id}")
        return MailIndexDeps(
            db=PinnedWorkspaceDatabase(
                db, episode_workspace=episode_workspace, index_workspace=handle.workspace_id
            ),
            clock=clock,
            ids=ids,
            embedder=self._embedder,
            query_stale=NEVER_STALE,  # 질의 직전 동기화 끔(고정 시점 스냅샷)
        )

    def config_info(self) -> dict[str, Any]:
        """결과의 설정 지문에 들어가는 색인 설정. 색인 지문에 세계·청크·토큰화·모델이 들어 있다."""
        return {
            "embedding_model_id": self._embedder.model_id,
            "fingerprints": {a: h.fingerprint for a, h in sorted(self._handles.items())},
            "index_now": worldmail.INDEX_NOW.isoformat(),
            "scope_labels": list(worldmail.SCOPE_LABELS),
            "scope_period_days": worldmail.SCOPE_PERIOD_DAYS,
            "sync_before_query": False,
        }

    def report(self) -> dict[str, Any]:
        return {a: h.to_dict() for a, h in sorted(self._handles.items())}


async def build_eval_indexes(
    *,
    world_snapshot: Mapping[str, Any],
    world_sha256: str,
    accounts: Sequence[str],
    app_url: str,
    owner_url: str,
    embedder: CachingEmbedder,
) -> tuple[EvalIndexProvider, float]:
    """계정마다 색인을 만들거나 재사용한다. (제공자, 걸린 초)를 돌려준다."""
    started = time.monotonic()
    world = SimWorld.load(dict(world_snapshot), FixedClock(worldmail.INDEX_NOW))
    unique = sorted(set(accounts))
    conns = build_sim_connectors(world, unique)
    db = AsyncDatabase.from_url(app_url, pool_size=2, max_overflow=2)
    try:
        indexer = EvalIndexer(
            db=db,
            owner_url=owner_url,
            embedder=embedder,
            world_sha256=world_sha256,
            expected_mails=lambda a: len(worldmail.mailbox_messages(world, a)),
            chunk_texts=lambda a: worldmail.mailbox_chunks(world, a),
        )
        handles = {
            a: await indexer.ensure(a, conns.mail[a], world.account(a).email) for a in unique
        }
    finally:
        await db.dispose()
    return EvalIndexProvider(handles, embedder), time.monotonic() - started
