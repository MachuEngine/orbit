"""A4R·A4RN local 메일 순위(ADR-0037): 질문-청크 최소 거리 상위 N통 -> reranker 재정렬.

DB 접근은 `workspace_id`를 `chunk`·`source_item` 양쪽에 같은 SQL 안에서 건다(D4-1, D4-4).
reranker 호출과 메일 본문 읽기는 호출자가 한다. 결과에는 제공자 ID만 담는다.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.evalrunner.rag_arch.a1 import rerank_order
from orbit.evalrunner.rag_arch.config import GRAPH_RERANK_CANDIDATES
from orbit.evalrunner.rag_arch.graph.prompts import LOCAL_MAX_MAILS
from orbit.mailindex.store import vector_literal

_DIST_SQL = (
    "SELECT si.provider_item_id AS pid, min(c.embedding <=> CAST(:emb AS vector)) AS d"
    " FROM chunk c JOIN source_item si ON si.id = c.source_item_id AND si.workspace_id = :ws"
    " WHERE c.workspace_id = :ws AND si.kind = 'mail'"
)
_IDS = " AND si.provider_item_id = ANY (CAST(:ids AS text[]))"
_GROUP = " GROUP BY si.provider_item_id"


async def mail_distances(
    session: AsyncSession,
    ws: uuid.UUID,
    embedding: list[float],
    candidates: Sequence[str] | None,
) -> dict[str, float]:
    """메일(제공자 ID)별 질문과 가장 가까운 청크의 코사인 거리. 청크 없는 메일은 빠진다."""
    params: dict[str, object] = {"ws": ws, "emb": vector_literal(embedding)}
    sql = _DIST_SQL
    if candidates is not None:
        sql += _IDS
        params["ids"] = list(candidates)
    rows = await session.execute(text(sql + _GROUP), params)
    return {r.pid: float(r.d) for r in rows}


def first_stage_order(candidates: Sequence[str] | None, dist: dict[str, float]) -> list[str]:
    """거리 오름차순, 같으면 제공자 ID 순. 청크 없는 후보는 맨 뒤(ID 순). None이면 `dist` 전체."""
    pool = dict.fromkeys(dist if candidates is None else candidates)
    return sorted(pool, key=lambda m: (dist.get(m, float("inf")), m))


async def first_stage(
    session: AsyncSession,
    ws: uuid.UUID,
    embedding: list[float],
    candidates: Sequence[str] | None,
) -> tuple[list[str], int]:
    """(reranker에 넣을 상위 GRAPH_RERANK_CANDIDATES통, 1단계 후보 수)."""
    ordered = first_stage_order(
        candidates, await mail_distances(session, ws, embedding, candidates)
    )
    return ordered[:GRAPH_RERANK_CANDIDATES], len(ordered)


def second_stage(head: Sequence[str], scores: Sequence[float]) -> list[str]:
    """reranker 점수 내림차순, 동점이면 1단계 순서. 상위 LOCAL_MAX_MAILS통."""
    if len(scores) != len(head):
        raise RuntimeError("reranker 점수 개수가 후보 수와 다르다")
    return [head[i] for i in rerank_order(scores, LOCAL_MAX_MAILS)]
