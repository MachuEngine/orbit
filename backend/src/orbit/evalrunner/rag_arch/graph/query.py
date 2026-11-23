"""A4 질의: 경로 선택(local/global), local 후보 메일 선정, global 요약 채우기(ADR-0036).

DB 접근은 `store`의 질의 함수만 쓰고(모두 `workspace_id` 필터), 이 모듈은 순수 계산과 LLM 1회
호출(경로 선택)만 한다. 결과·진단에는 ID와 숫자만 담는다.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from orbit.assistant.agent import TEMPERATURE
from orbit.evalrunner.rag_arch.graph import store
from orbit.evalrunner.rag_arch.graph.names import squash
from orbit.evalrunner.rag_arch.graph.prompts import (
    EMBED_MATCH_LIMIT,
    GLOBAL_MAX_COMMUNITIES,
    HOPS,
    LOCAL_MAX_MAILS,
    NAME_MATCH_LIMIT,
    ROUTE_MAX_OUTPUT_TOKENS,
    ROUTE_PROMPT,
)
from orbit.evalrunner.rag_arch.graph.store import CommunityHit, RelationRow
from orbit.llm.interface import LLM
from orbit.llm.types import LLMRequest, Message

MODES = ("local", "global")
FALLBACK_MODE = "local"  # 경로 선택이 실패하면 local로 간다(진단에 `route_fallback`을 남긴다)


def parse_route(text: str | None) -> str | None:
    """`{"mode":"local"|"global"}`만 받는다. 그 밖은 None."""
    try:
        data = json.loads((text or "").strip())
    except ValueError:
        return None
    mode = data.get("mode") if isinstance(data, dict) else None
    return mode if mode in MODES else None


async def route_question(llm: LLM, question: str, task_id: str) -> tuple[str, bool]:
    """질문 문장만 보고 경로를 고른다. 반환은 (모드, 폴백 여부)."""
    response = await llm.complete(
        LLMRequest(
            messages=(
                Message(role="system", content=ROUTE_PROMPT),
                Message(role="user", content=question),
            ),
            purpose="system_under_test",
            max_output_tokens=ROUTE_MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
            metadata={"task_id": task_id, "step_kind": "a4_route"},
        )
    )
    mode = None
    if not response.tool_calls and response.finish_reason != "length":
        mode = parse_route(response.text)
    return (mode or FALLBACK_MODE), mode is None


@dataclass(frozen=True)
class LocalPlan:
    mail_ids: list[str]  # 순위대로, LOCAL_MAX_MAILS통까지(제공자 ID)
    hops: list[tuple[int, RelationRow]]  # (홉, 관계). 관계 블록용
    seed_count: int
    name_matches: int
    embed_matches: int
    all_mail_ids: Sequence[str] = ()  # 자르기 전 도달 메일 전체(같은 순위). A4R의 후보(ADR-0037)


def rank_all_mails(hops: Sequence[tuple[int, RelationRow]]) -> list[str]:
    """메일 순위: (가장 가까운 홉, 그 홉의 관계 수 내림차순, 제공자 ID). 자르지 않은 전체."""
    best: dict[str, tuple[int, int]] = {}
    for hop, rel in hops:
        cur = best.get(rel.provider_item_id)
        if cur is None or hop < cur[0]:
            best[rel.provider_item_id] = (hop, 1)
        elif hop == cur[0]:
            best[rel.provider_item_id] = (hop, cur[1] + 1)
    return sorted(best, key=lambda m: (best[m][0], -best[m][1], m))


def rank_mails(hops: Sequence[tuple[int, RelationRow]]) -> list[str]:
    """`rank_all_mails`의 상위 LOCAL_MAX_MAILS통."""
    return rank_all_mails(hops)[:LOCAL_MAX_MAILS]


async def plan_local(
    session: AsyncSession, ws: uuid.UUID, question: str, embedding: list[float]
) -> LocalPlan:
    """시드 개체(이름 일치 + 임베딩 근접)에서 HOPS홉까지 관계를 모으고 메일을 순위 매긴다."""
    named = await store.match_entities_by_name(session, ws, squash(question), NAME_MATCH_LIMIT)
    near = await store.nearest_entities(session, ws, embedding, EMBED_MATCH_LIMIT)
    seeds = {e.id: e for e in [*named, *near]}
    seen: set[uuid.UUID] = set(seeds)
    frontier = list(seeds)
    hops: list[tuple[int, RelationRow]] = []
    for hop in range(1, HOPS + 1):
        if not frontier:
            break
        relations = await store.neighbor_relations(session, ws, frontier)
        hops.extend((hop, r) for r in relations)
        nxt = {r.subject_id for r in relations} | {r.object_id for r in relations if r.object_id}
        frontier = sorted(nxt - seen)
        seen |= nxt
    everything = rank_all_mails(hops)
    return LocalPlan(
        everything[:LOCAL_MAX_MAILS], hops, len(seeds), len(named), len(near), everything
    )


def relation_lines(plan: LocalPlan, handles: dict[str, str]) -> list[str]:
    """모델 입력에 들어간 메일(핸들이 있는 것)의 관계를 `[M3] 주어 | 관계 | 객체`로 만든다."""
    seen: set[tuple[str, str, str, str]] = set()
    lines: list[str] = []
    order = {m: i for i, m in enumerate(plan.mail_ids)}
    for _hop, rel in sorted(
        plan.hops, key=lambda t: (t[0], order.get(t[1].provider_item_id, 1 << 30), str(t[1].id))
    ):
        handle = handles.get(rel.provider_item_id)
        if handle is None:
            continue
        key = (handle, rel.subject_name, rel.predicate, rel.object_name or "")
        if key not in seen:
            seen.add(key)
            lines.append(f"[{handle}] {rel.subject_name} | {rel.predicate} | {rel.object_name}")
    return lines


async def plan_global(
    session: AsyncSession, ws: uuid.UUID, embedding: list[float]
) -> list[CommunityHit]:
    """질문 임베딩과 요약 임베딩이 가까운 커뮤니티 상위 GLOBAL_MAX_COMMUNITIES개."""
    return await store.nearest_communities(session, ws, embedding, GLOBAL_MAX_COMMUNITIES)
