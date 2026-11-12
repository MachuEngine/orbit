"""메일 색인 검색 (ADR-0019 §5, D4-1·D4-4, `실험 중`).

SQL 한 문장(CTE)에서 다음을 모두 한다.
1. `workspace_id`(와 그 워크스페이스의 활성 연결)로 거른 뒤 벡터 거리 상위 `VECTOR_CANDIDATES`개,
   키워드 점수(`ts_rank`) 상위 `KEYWORD_CANDIDATES`개를 뽑는다.
2. 두 순위를 RRF(`1/(k+순위)`, k=`RRF_K`)로 합친다.
3. 메일 단위로 묶어 가장 점수가 높은 청크를 대표로 하고, 점수순 상위 N통을 돌려준다.
RLS가 같은 `workspace_id` 조건을 DB에서 한 번 더 강제한다. 필터와 HNSW를 함께 쓸 때 결과가 모자라지
않도록 트랜잭션 안에서 `hnsw.iterative_scan = relaxed_order`를 켜고, 반복 스캔이 순서를 어긋나게 할
수 있어 후보를 거리순으로 다시 정렬한다. `ts_rank`는 BM25가 아니다. 질의 토큰이 없으면 벡터만, 질의
벡터가 없으면(임베딩 실패) 키워드만 쓴다.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.mailindex.limits import (
    HNSW_EF_SEARCH,
    KEYWORD_CANDIDATES,
    MAX_QUERY_TOKENS,
    RRF_K,
    VECTOR_CANDIDATES,
)
from orbit.mailindex.store import vector_literal
from orbit.mailindex.tokenize import normalize_token

_ACTIVE = (
    "active AS MATERIALIZED (SELECT id FROM connection WHERE workspace_id = :ws"
    " AND status = 'active')"
)
_SCOPE = "c.workspace_id = :ws AND c.connection_id = ANY (ARRAY(SELECT id FROM active))"

_VECTOR_CTES = """
vec_raw AS MATERIALIZED (
    SELECT c.id, c.source_item_id, c.embedding <=> CAST(:emb AS vector) AS dist
    FROM chunk c WHERE __SCOPE__
    ORDER BY c.embedding <=> CAST(:emb AS vector)
    LIMIT :vec_n
),
vec AS (
    SELECT id, source_item_id, row_number() OVER (ORDER BY dist, id) AS rnk FROM vec_raw
)"""
_KEYWORD_CTES = """
kw AS (
    SELECT c.id, c.source_item_id,
           row_number() OVER (ORDER BY ts_rank(c.tokens, q.query) DESC, c.id) AS rnk
    FROM chunk c, (SELECT to_tsquery('simple', :tsq) AS query) q
    WHERE __SCOPE__ AND c.tokens @@ q.query
    ORDER BY ts_rank(c.tokens, q.query) DESC, c.id
    LIMIT :kw_n
)"""
_TAIL = """
SELECT si.provider_item_id, si.connection_id, si.thread_id, si.subject, si.sender_name,
       si.sender_email, si.sent_at, ch.content, b.score
FROM best b
JOIN source_item si ON si.id = b.source_item_id AND si.workspace_id = :ws
JOIN chunk ch ON ch.id = b.chunk_id AND ch.workspace_id = :ws
WHERE si.kind = 'mail'
ORDER BY b.score DESC, si.sent_at DESC, si.id
LIMIT :n
"""


@dataclass(frozen=True)
class SearchHit:
    provider_item_id: str
    connection_id: uuid.UUID
    thread_id: str
    subject: str
    sender_name: str | None
    sender_email: str
    sent_at: datetime
    excerpt: str
    score: float


def to_tsquery_text(tokens: list[str]) -> str:
    """토큰을 `to_tsquery('simple', ...)`용 OR 질의로 만든다. 따옴표로 감싸 연산자가 되지 않는다."""
    seen: dict[str, None] = {}
    for token in tokens:
        clean = normalize_token(token)
        if clean:
            seen[clean] = None
    return " | ".join(f"'{t}'" for t in list(seen)[:MAX_QUERY_TOKENS])


def build_search_sql(*, with_vector: bool, with_keyword: bool) -> str:
    """검색 SQL. 조각은 모두 이 모듈의 상수이고 사용자 입력은 바인드 인자로만 들어간다."""
    if not (with_vector or with_keyword):
        raise ValueError("벡터와 키워드 중 하나는 있어야 한다")
    ctes = [_ACTIVE]
    parts: list[str] = []
    if with_vector:
        ctes.append(_VECTOR_CTES.strip().replace("__SCOPE__", _SCOPE))
        parts.append("SELECT id, source_item_id, rnk FROM vec")
    if with_keyword:
        ctes.append(_KEYWORD_CTES.strip().replace("__SCOPE__", _SCOPE))
        parts.append("SELECT id, source_item_id, rnk FROM kw")
    ctes.append(
        "fused AS (SELECT id, source_item_id, sum(1.0 / (CAST(:rrf_k AS integer) + rnk)) AS score"  # noqa: S608
        f" FROM ({' UNION ALL '.join(parts)}) u GROUP BY id, source_item_id)"
    )
    ctes.append(
        "best AS (SELECT DISTINCT ON (f.source_item_id) f.source_item_id, f.id AS chunk_id,"
        " f.score FROM fused f ORDER BY f.source_item_id, f.score DESC, f.id)"
    )
    return "WITH " + ",\n".join(ctes) + _TAIL


async def search_index(
    session: AsyncSession,
    ws: uuid.UUID,
    *,
    embedding: list[float] | None,
    tokens: list[str],
    limit: int,
) -> list[SearchHit]:
    """하이브리드 검색. 호출자가 연 워크스페이스 세션(트랜잭션) 안에서 실행한다."""
    tsq = to_tsquery_text(tokens)
    with_vector = embedding is not None
    with_keyword = bool(tsq)
    if not (with_vector or with_keyword):
        return []
    params: dict[str, object] = {"ws": ws, "rrf_k": RRF_K, "n": limit}
    if embedding is not None:
        params["emb"] = vector_literal(embedding)
        params["vec_n"] = VECTOR_CANDIDATES
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(HNSW_EF_SEARCH)}"))
    if with_keyword:
        params["tsq"] = tsq
        params["kw_n"] = KEYWORD_CANDIDATES
    sql = build_search_sql(with_vector=with_vector, with_keyword=with_keyword)
    rows = await session.execute(text(sql), params)
    return [
        SearchHit(
            provider_item_id=r.provider_item_id,
            connection_id=r.connection_id,
            thread_id=r.thread_id,
            subject=r.subject,
            sender_name=r.sender_name,
            sender_email=r.sender_email,
            sent_at=r.sent_at,
            excerpt=r.content,
            score=float(r.score),
        )
        for r in rows
    ]
