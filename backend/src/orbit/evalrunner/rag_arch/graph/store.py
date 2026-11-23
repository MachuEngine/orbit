"""메일 그래프 테이블 접근(마이그레이션 0013). 모든 쿼리는 같은 SQL에 `workspace_id = :ws`를 둔다.

세션은 호출자가 연 워크스페이스 세션이다. 구축은 색인 워크스페이스로, 질의는
`PinnedWorkspaceDatabase` 세션으로 부른다(후자는 `:ws` 인자를 색인 워크스페이스로 바꿔 준다).
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.core.ids import Uuid7Generator
from orbit.evalrunner.rag_arch.graph.extract import Extraction, Relation
from orbit.mailindex.store import vector_literal


@dataclass(frozen=True)
class CallCost:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int


@dataclass(frozen=True)
class EntityRow:
    id: uuid.UUID
    name_key: str
    name: str
    kind: str


@dataclass(frozen=True)
class RelationRow:
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    predicate: str
    object_id: uuid.UUID | None
    object_name: str | None  # 개체 이름 또는 값
    object_value: str | None
    source_item_id: uuid.UUID
    provider_item_id: str


_RELATION_SELECT = (
    "SELECT r.id, r.subject_entity_id, se.name AS s_name, r.predicate, r.object_entity_id,"
    " COALESCE(oe.name, r.object_value) AS o_name, r.object_value, r.source_item_id,"
    " si.provider_item_id"
    " FROM mail_graph_relation r"
    " JOIN mail_graph_entity se ON se.id = r.subject_entity_id AND se.workspace_id = :ws"
    " LEFT JOIN mail_graph_entity oe ON oe.id = r.object_entity_id AND oe.workspace_id = :ws"
    " JOIN source_item si ON si.id = r.source_item_id AND si.workspace_id = :ws"
    " WHERE r.workspace_id = :ws"
)


def _relation(r: Any) -> RelationRow:
    return RelationRow(
        r.id, r.subject_entity_id, r.s_name, r.predicate, r.object_entity_id, r.o_name,
        r.object_value, r.source_item_id, r.provider_item_id,
    )  # fmt: skip


def _uuids(values: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    return list(values)


# ---------------------------------------------------------------- 구축


async def source_item_ids(session: AsyncSession, ws: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = await session.execute(
        text(
            "SELECT provider_item_id, id FROM source_item"
            " WHERE workspace_id = :ws AND kind = 'mail'"
        ),
        {"ws": ws},
    )
    return {r.provider_item_id: r.id for r in rows}


async def extraction_tags(session: AsyncSession, ws: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        text(
            "SELECT source_item_id, config_tag FROM mail_graph_extraction WHERE workspace_id = :ws"
        ),
        {"ws": ws},
    )
    return {r.source_item_id: r.config_tag for r in rows}


async def community_tags(session: AsyncSession, ws: uuid.UUID) -> dict[str, str]:
    rows = await session.execute(
        text("SELECT community_key, config_tag FROM mail_graph_community WHERE workspace_id = :ws"),
        {"ws": ws},
    )
    return {r.community_key: r.config_tag for r in rows}


async def get_build(session: AsyncSession, ws: uuid.UUID) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT graph_digest, config, stats, built_at FROM mail_graph_build"
                " WHERE workspace_id = :ws"
            ),
            {"ws": ws},
        )
    ).one_or_none()
    if row is None:
        return None
    return {
        "graph_digest": row.graph_digest,
        "config": row.config,
        "stats": row.stats,
        "built_at": row.built_at,
    }


async def save_build(
    session: AsyncSession,
    ws: uuid.UUID,
    digest: str,
    config: dict[str, Any],
    stats: dict[str, Any],
    now: datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO mail_graph_build (workspace_id, graph_digest, config, stats, built_at)"
            " VALUES (:ws, :d, CAST(:c AS jsonb), CAST(:s AS jsonb), :now)"
            " ON CONFLICT (workspace_id) DO UPDATE SET graph_digest = EXCLUDED.graph_digest,"
            " config = EXCLUDED.config, stats = EXCLUDED.stats, built_at = EXCLUDED.built_at"
        ),
        {
            "ws": ws,
            "d": digest,
            "c": json.dumps(config, sort_keys=True),
            "s": json.dumps(stats, sort_keys=True),
            "now": now,
        },
    )


async def reset_graph(session: AsyncSession, ws: uuid.UUID) -> None:
    """이 워크스페이스의 그래프를 모두 지운다(`--rebuild`). 색인(source_item·chunk)은 그대로다."""
    for table in (
        "mail_graph_build",
        "mail_graph_community",
        "mail_graph_extraction",
        "mail_graph_relation",
        "mail_graph_entity",
    ):
        await session.execute(text(f"DELETE FROM {table} WHERE workspace_id = :ws"), {"ws": ws})  # noqa: S608


async def save_extraction(
    session: AsyncSession,
    ws: uuid.UUID,
    ids: Uuid7Generator,
    now: datetime,
    *,
    source_id: uuid.UUID,
    result: Extraction | None,
    cost: CallCost,
    config_tag: str,
) -> None:
    """한 메일의 추출 결과를 한 트랜잭션으로 저장한다. `result`가 None이면 형식 실패만 남긴다."""
    entity_ids: dict[str, uuid.UUID] = {}
    relations: tuple[Relation, ...] = ()
    if result is not None:
        relations = result.relations
        for key, (name, kind) in sorted(result.entities.items()):
            found = (
                await session.execute(
                    text(
                        "INSERT INTO mail_graph_entity (id, workspace_id, name_key, name, kind,"
                        " source_item_ids, created_at) VALUES (:id, :ws, :key, :name, :kind,"
                        " ARRAY[CAST(:sid AS uuid)], :now)"
                        " ON CONFLICT (workspace_id, name_key) DO UPDATE SET source_item_ids ="
                        " CASE WHEN CAST(:sid AS uuid) = ANY (mail_graph_entity.source_item_ids)"
                        " THEN mail_graph_entity.source_item_ids"
                        " ELSE mail_graph_entity.source_item_ids || CAST(:sid AS uuid) END"
                        " RETURNING id"
                    ),
                    {
                        "id": ids.next(),
                        "ws": ws,
                        "key": key,
                        "name": name,
                        "kind": kind,
                        "sid": source_id,
                        "now": now,
                    },
                )
            ).scalar_one()
            entity_ids[key] = uuid.UUID(str(found))
        for rel in relations:
            await session.execute(
                text(
                    "INSERT INTO mail_graph_relation (id, workspace_id, source_item_id,"
                    " subject_entity_id, predicate, object_entity_id, object_value)"
                    " VALUES (:id, :ws, :sid, :s, :p, :o, :v)"
                ),
                {
                    "id": ids.next(),
                    "ws": ws,
                    "sid": source_id,
                    "s": entity_ids[rel.subject_key],
                    "p": rel.predicate,
                    "o": None if rel.object_key is None else entity_ids[rel.object_key],
                    "v": rel.object_value,
                },
            )
    await session.execute(
        text(
            "INSERT INTO mail_graph_extraction (source_item_id, workspace_id, status, config_tag,"
            " entity_count, relation_count, dropped_relations, input_tokens, output_tokens,"
            " cost_usd, latency_ms, created_at) VALUES (:sid, :ws, :status, :tag, :ec, :rc, :dr,"
            " :it, :ot, :cost, :lat, :now)"
        ),
        {
            "sid": source_id,
            "ws": ws,
            "status": "ok" if result is not None else "format_error",
            "tag": config_tag,
            "ec": 0 if result is None else len(result.entities),
            "rc": len(relations),
            "dr": 0 if result is None else result.dropped,
            "it": cost.input_tokens,
            "ot": cost.output_tokens,
            "cost": cost.cost_usd,
            "lat": cost.latency_ms,
            "now": now,
        },
    )


async def entities_without_embedding(session: AsyncSession, ws: uuid.UUID) -> list[EntityRow]:
    rows = await session.execute(
        text(
            "SELECT id, name_key, name, kind FROM mail_graph_entity"
            " WHERE workspace_id = :ws AND embedding IS NULL ORDER BY name_key"
        ),
        {"ws": ws},
    )
    return [EntityRow(r.id, r.name_key, r.name, r.kind) for r in rows]


async def set_entity_embeddings(
    session: AsyncSession,
    ws: uuid.UUID,
    rows: Sequence[EntityRow],
    vectors: Sequence[list[float]],
    model_id: str,
) -> None:
    for row, vector in zip(rows, vectors, strict=True):
        await session.execute(
            text(
                "UPDATE mail_graph_entity SET embedding = CAST(:e AS vector), embedding_model = :m"
                " WHERE workspace_id = :ws AND id = :id"
            ),
            {"e": vector_literal(vector), "m": model_id, "ws": ws, "id": row.id},
        )


async def all_entities(session: AsyncSession, ws: uuid.UUID) -> list[EntityRow]:
    rows = await session.execute(
        text(
            "SELECT id, name_key, name, kind FROM mail_graph_entity WHERE workspace_id = :ws"
            " ORDER BY name_key"
        ),
        {"ws": ws},
    )
    return [EntityRow(r.id, r.name_key, r.name, r.kind) for r in rows]


async def all_relations(session: AsyncSession, ws: uuid.UUID) -> list[RelationRow]:
    rows = await session.execute(
        text(_RELATION_SELECT + " ORDER BY si.provider_item_id, r.predicate, r.id"), {"ws": ws}
    )
    return [_relation(r) for r in rows]


async def save_community(
    session: AsyncSession,
    ws: uuid.UUID,
    ids: Uuid7Generator,
    now: datetime,
    *,
    key: str,
    entity_ids: Sequence[uuid.UUID],
    source_ids: Sequence[uuid.UUID],
    summary: str,
    config_tag: str,
    cost: CallCost,
) -> None:
    """요약만 먼저 저장한다. 임베딩은 별도 단계(`set_community_embeddings`)에서 채운다."""
    await session.execute(
        text(
            "INSERT INTO mail_graph_community (id, workspace_id, community_key, entity_ids,"
            " source_item_ids, summary, config_tag,"
            " input_tokens, output_tokens, cost_usd, latency_ms, created_at)"
            " VALUES (:id, :ws, :key, CAST(:eids AS uuid[]), CAST(:sids AS uuid[]), :summary,"
            " :tag, :it, :ot, :cost, :lat, :now)"
        ),
        {
            "id": ids.next(),
            "ws": ws,
            "key": key,
            "eids": _uuids(entity_ids),
            "sids": _uuids(source_ids),
            "summary": summary,
            "tag": config_tag,
            "it": cost.input_tokens,
            "ot": cost.output_tokens,
            "cost": cost.cost_usd,
            "lat": cost.latency_ms,
            "now": now,
        },
    )


async def communities_without_embedding(
    session: AsyncSession, ws: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    rows = await session.execute(
        text(
            "SELECT id, summary FROM mail_graph_community"
            " WHERE workspace_id = :ws AND summary_embedding IS NULL ORDER BY community_key"
        ),
        {"ws": ws},
    )
    return [(r.id, r.summary) for r in rows]


async def set_community_embeddings(
    session: AsyncSession,
    ws: uuid.UUID,
    rows: Sequence[tuple[uuid.UUID, str]],
    vectors: Sequence[list[float]],
    model_id: str,
) -> None:
    for (cid, _), vector in zip(rows, vectors, strict=True):
        await session.execute(
            text(
                "UPDATE mail_graph_community SET summary_embedding = CAST(:e AS vector),"
                " embedding_model = :m WHERE workspace_id = :ws AND id = :id"
            ),
            {"e": vector_literal(vector), "m": model_id, "ws": ws, "id": cid},
        )


async def entity_embedding_models(session: AsyncSession, ws: uuid.UUID) -> list[str]:
    """이미 임베딩이 저장된 개체가 쓴 임베딩 모델 ID(중복 없음)."""
    rows = await session.execute(
        text(
            "SELECT DISTINCT embedding_model FROM mail_graph_entity"
            " WHERE workspace_id = :ws AND embedding IS NOT NULL"
        ),
        {"ws": ws},
    )
    return sorted(r.embedding_model or "" for r in rows)


async def community_summaries(session: AsyncSession, ws: uuid.UUID) -> list[tuple[str, str]]:
    rows = await session.execute(
        text(
            "SELECT community_key, summary FROM mail_graph_community WHERE workspace_id = :ws"
            " ORDER BY community_key"
        ),
        {"ws": ws},
    )
    return [(r.community_key, r.summary) for r in rows]


async def entity_mails(
    session: AsyncSession, ws: uuid.UUID
) -> list[tuple[str, str, tuple[str, ...]]]:
    """(이름, 종류, 출처 메일 제공자 ID들). 추출 품질 계산용."""
    rows = await session.execute(
        text(
            "SELECT e.name, e.kind, array_agg(si.provider_item_id ORDER BY si.provider_item_id)"
            " AS mails FROM mail_graph_entity e JOIN source_item si ON si.workspace_id = :ws"
            " AND si.id = ANY (e.source_item_ids) WHERE e.workspace_id = :ws"
            " GROUP BY e.id, e.name, e.kind ORDER BY e.name_key"
        ),
        {"ws": ws},
    )
    return [(r.name, r.kind, tuple(r.mails)) for r in rows]


async def cost_rows(session: AsyncSession, ws: uuid.UUID) -> dict[str, Any]:
    """추출·요약 비용 합계와 메일별 갱신 비용 계산용 원자료. 본문은 없다."""
    ext = (
        await session.execute(
            text(
                "SELECT source_item_id, status, input_tokens, output_tokens, cost_usd, latency_ms"
                " FROM mail_graph_extraction WHERE workspace_id = :ws ORDER BY source_item_id"
            ),
            {"ws": ws},
        )
    ).all()
    com = (
        await session.execute(
            text(
                "SELECT source_item_ids, input_tokens, output_tokens, cost_usd, latency_ms"
                " FROM mail_graph_community WHERE workspace_id = :ws ORDER BY community_key"
            ),
            {"ws": ws},
        )
    ).all()
    return {"extractions": ext, "communities": com}


# ---------------------------------------------------------------- 질의


async def graph_counts(session: AsyncSession, ws: uuid.UUID) -> tuple[int, int]:
    """(개체 수, 커뮤니티 수). 그래프가 없는 색인에서 질의하는 것을 막는 데 쓴다."""
    row = (
        await session.execute(
            text(
                "SELECT (SELECT count(*) FROM mail_graph_entity WHERE workspace_id = :ws) AS e,"
                " (SELECT count(*) FROM mail_graph_community WHERE workspace_id = :ws) AS c"
            ),
            {"ws": ws},
        )
    ).one()
    return int(row.e), int(row.c)


async def match_entities_by_name(
    session: AsyncSession, ws: uuid.UUID, question_key: str, limit: int
) -> list[EntityRow]:
    """정규화 이름이 질문 문장(공백·문장부호 제거)에 그대로 들어 있는 개체. 긴 이름 먼저."""
    rows = await session.execute(
        text(
            "SELECT id, name_key, name, kind FROM mail_graph_entity"
            " WHERE workspace_id = :ws AND length(name_key) >= 2"
            " AND position(name_key IN :q) > 0"
            " ORDER BY length(name_key) DESC, name_key LIMIT :n"
        ),
        {"ws": ws, "q": question_key, "n": limit},
    )
    return [EntityRow(r.id, r.name_key, r.name, r.kind) for r in rows]


async def nearest_entities(
    session: AsyncSession, ws: uuid.UUID, embedding: list[float], limit: int
) -> list[EntityRow]:
    rows = await session.execute(
        text(
            "SELECT id, name_key, name, kind FROM mail_graph_entity"
            " WHERE workspace_id = :ws AND embedding IS NOT NULL"
            " ORDER BY embedding <=> CAST(:emb AS vector), name_key LIMIT :n"
        ),
        {"ws": ws, "emb": vector_literal(embedding), "n": limit},
    )
    return [EntityRow(r.id, r.name_key, r.name, r.kind) for r in rows]


async def neighbor_relations(
    session: AsyncSession, ws: uuid.UUID, entity_ids: Sequence[uuid.UUID]
) -> list[RelationRow]:
    """주어진 개체가 주어이거나 개체 객체인 관계."""
    rows = await session.execute(
        text(
            _RELATION_SELECT + " AND (r.subject_entity_id = ANY (CAST(:ids AS uuid[]))"
            " OR r.object_entity_id = ANY (CAST(:ids AS uuid[])))"
            " ORDER BY si.provider_item_id, r.predicate, r.id"
        ),
        {"ws": ws, "ids": _uuids(entity_ids)},
    )
    return [_relation(r) for r in rows]


@dataclass(frozen=True)
class CommunityHit:
    id: uuid.UUID
    summary: str
    provider_item_ids: tuple[str, ...]


async def nearest_communities(
    session: AsyncSession, ws: uuid.UUID, embedding: list[float], limit: int
) -> list[CommunityHit]:
    """요약 임베딩이 질문과 가까운 커뮤니티와 출처 메일(제공자 ID)."""
    rows = await session.execute(
        text(
            "WITH top AS (SELECT id, summary, source_item_ids, summary_embedding <=>"
            " CAST(:emb AS vector) AS dist FROM mail_graph_community WHERE workspace_id = :ws"
            " AND summary_embedding IS NOT NULL ORDER BY dist, id LIMIT :n)"
            " SELECT t.id, t.summary, t.dist, COALESCE(array_agg(si.provider_item_id"
            " ORDER BY si.provider_item_id) FILTER (WHERE si.id IS NOT NULL), '{}') AS mails"
            " FROM top t LEFT JOIN source_item si ON si.workspace_id = :ws"
            " AND si.id = ANY (t.source_item_ids)"
            " GROUP BY t.id, t.summary, t.dist ORDER BY t.dist, t.id"
        ),
        {"ws": ws, "emb": vector_literal(embedding), "n": limit},
    )
    return [CommunityHit(r.id, r.summary, tuple(r.mails)) for r in rows]
