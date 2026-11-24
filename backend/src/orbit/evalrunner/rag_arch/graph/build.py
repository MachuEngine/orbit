"""A4 그래프 구축(ADR-0036 A4, 평가 전용): 추출 -> 개체 병합 -> 개체 임베딩 -> 커뮤니티 -> 요약.

- 색인 워크스페이스 하나(계정 하나)씩 구축한다. 메일 한 통의 추출은 한 트랜잭션이라 중단 뒤 다시
  실행하면 끝난 메일은 건너뛴다(재개). 커뮤니티는 개체 키 해시로 식별하고 Louvain이 고정 시드라
  재개 때 같은 묶음이 나온다. 추출·요약 설정(프롬프트 sha, 모델 ID)이 다른 행이 있으면 거부한다.
- LLM 호출은 `BUILD_CONCURRENCY`개씩 동시에 하고 결과는 입력 순서대로 저장한다(개체 표시 이름·종류가
  동시 실행 순서에 따라 달라지지 않게 한다).
- 누적 비용 상한을 넘으면 `BudgetStop`으로 멈춘다(저장된 부분은 남고 재개할 수 있다).
"""

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from orbit.assistant.agent import TEMPERATURE
from orbit.assistant.envelope import boundary_for, wrap_data
from orbit.assistant.prompt import data_rule_lines
from orbit.connectors.contracts import MailMessage
from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.evalrunner.metrics_v1 import percentile
from orbit.evalrunner.rag_arch.config import prompt_sha
from orbit.evalrunner.rag_arch.graph import store
from orbit.evalrunner.rag_arch.graph.extract import Extraction, parse_extraction, render_mail
from orbit.evalrunner.rag_arch.graph.prompts import (
    BUILD_CONCURRENCY,
    EXTRACT_MAX_OUTPUT_TOKENS,
    EXTRACT_PROMPT,
    EXTRACT_VERSION,
    LOUVAIN_SEED,
    MAX_ENTITIES_PER_MAIL,
    MAX_NAME_CHARS,
    MAX_PREDICATE_CHARS,
    MAX_RELATIONS_PER_MAIL,
    MAX_SUMMARY_RELATIONS,
    MIN_COMMUNITY_ENTITIES,
    SUMMARY_MAX_OUTPUT_TOKENS,
    SUMMARY_PROMPT,
    SUMMARY_VERSION,
)
from orbit.evalrunner.rag_arch.graph.store import CallCost
from orbit.llm.interface import LLM
from orbit.llm.types import LLMRequest, LLMResponse, Message
from orbit.mailindex.embedding import Embedder
from orbit.mailindex.limits import EMBED_BATCH_SIZE


class BudgetStop(RuntimeError):  # noqa: N818  도메인 용어
    """누적 비용 상한에 도달해 멈췄다. 저장된 부분은 남아 있어 다시 실행하면 이어서 한다."""


class GraphConfigConflict(RuntimeError):  # noqa: N818  도메인 용어
    """이미 있는 그래프 행이 현재 추출·요약 설정과 다르다. `--rebuild`로 지우고 다시 만든다."""


class IndexMismatch(RuntimeError):  # noqa: N818  도메인 용어
    """세계의 메일이 색인에 없다. 색인을 먼저 만들어야 한다."""


class PriceSettings(BaseSettings):
    """`ORBIT_LLM_PRICE_*` 단가표(1백만 토큰당 달러). 응답에 비용이 없을 때와 dry-run에 쓴다."""

    model_config = SettingsConfigDict(env_prefix="ORBIT_", extra="ignore")

    llm_price_input_per_mtok: float | None = Field(default=None, ge=0)
    llm_price_output_per_mtok: float | None = Field(default=None, ge=0)


def token_cost(price: PriceSettings | None, input_tokens: int, output_tokens: int) -> float | None:
    if price is None or price.llm_price_input_per_mtok is None:
        return None
    if price.llm_price_output_per_mtok is None:
        return None
    return round(
        (input_tokens * price.llm_price_input_per_mtok
         + output_tokens * price.llm_price_output_per_mtok) / 1_000_000,
        8,
    )  # fmt: skip


def _limits_sha(*values: int) -> str:
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:8]


def extract_limits_sha() -> str:
    """추출 결과에 영향을 주는 상수의 지문. 값이 바뀌면 이어서 만들지 않는다."""
    return _limits_sha(
        MAX_ENTITIES_PER_MAIL,
        MAX_RELATIONS_PER_MAIL,
        MAX_NAME_CHARS,
        MAX_PREDICATE_CHARS,
        EXTRACT_MAX_OUTPUT_TOKENS,
    )


def summary_limits_sha() -> str:
    return _limits_sha(MAX_SUMMARY_RELATIONS, SUMMARY_MAX_OUTPUT_TOKENS)


def extract_tag(model_id: str) -> str:
    return f"{EXTRACT_VERSION}:{prompt_sha(EXTRACT_PROMPT)}:{model_id}:{extract_limits_sha()}"


def summary_tag(model_id: str, embedding_model: str) -> str:
    sha = prompt_sha(SUMMARY_PROMPT)
    return (
        f"{SUMMARY_VERSION}:{sha}:{model_id}:{embedding_model}"
        f":{LOUVAIN_SEED}:{MIN_COMMUNITY_ENTITIES}:{summary_limits_sha()}"
    )


def detect_communities(
    keys: Sequence[str], edges: Sequence[tuple[str, str]]
) -> list[tuple[str, ...]]:
    """Louvain(networkx, 고정 시드) 커뮤니티. 입력 순서와 무관하게 같은 결과를 낸다.

    노드·간선을 정렬해 넣고, 각 커뮤니티의 키를 정렬한 뒤 첫 키 순으로 돌려준다. 간선 가중치는 두
    개체 사이 관계 수다.
    """
    try:
        import networkx as nx  # noqa: PLC0415  평가 전용 의존성(`--group rag-eval`)
    except ImportError as exc:
        raise RuntimeError("networkx가 필요하다: uv sync --group rag-eval") from exc
    graph = nx.Graph()
    graph.add_nodes_from(sorted(set(keys)))
    weights: dict[tuple[str, str], int] = {}
    for a, b in edges:
        if a != b:
            pair = (a, b) if a < b else (b, a)
            weights[pair] = weights.get(pair, 0) + 1
    for (a, b), w in sorted(weights.items()):
        graph.add_edge(a, b, weight=w)
    found = nx.community.louvain_communities(graph, weight="weight", seed=LOUVAIN_SEED)
    return sorted(tuple(sorted(c)) for c in found)


def community_key(keys: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()[:32]


@dataclass
class Spend:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unknown_cost_calls: int = 0
    latency_ms: int = 0

    def add(self, c: CallCost) -> None:
        self.calls += 1
        self.input_tokens += c.input_tokens
        self.output_tokens += c.output_tokens
        self.latency_ms += c.latency_ms
        if c.cost_usd is None:
            self.unknown_cost_calls += 1
        else:
            self.cost_usd += c.cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "cost_usd": round(self.cost_usd, 8)}


@dataclass
class BuildReport:
    reused: bool = False
    graph_digest: str = ""
    extraction: Spend = field(default_factory=Spend)  # 이번 실행에서 부른 것만
    summary: Spend = field(default_factory=Spend)
    format_errors: int = 0  # 이번 실행에서 형식 실패한 메일 수
    extracted_mails: int = 0
    skipped_mails: int = 0  # 이전 실행에서 끝난 메일
    summary_failures: int = 0
    seconds: float = 0.0
    totals: dict[str, Any] = field(default_factory=dict)  # DB 기준 전체(재개 합산)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reused": self.reused,
            "graph_digest": self.graph_digest,
            "this_run": {
                "extraction": self.extraction.to_dict(),
                "summary": self.summary.to_dict(),
                "format_errors": self.format_errors,
                "extracted_mails": self.extracted_mails,
                "skipped_mails": self.skipped_mails,
                "summary_failures": self.summary_failures,
                "seconds": round(self.seconds, 3),
            },
            "totals": self.totals,
        }


def _cost(response: LLMResponse, price: PriceSettings | None) -> CallCost:
    u = response.usage
    cost = response.cost_usd
    if cost is None:
        cost = token_cost(price, u.input_tokens, u.output_tokens)
    return CallCost(u.input_tokens, u.output_tokens, cost, response.latency_ms)


def _windows(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class _Guard:
    """누적 비용 상한. 호출 전에 예상 비용을 확인하고, 응답은 저장한 뒤에 상한을 검사한다."""

    def __init__(self, limit: float | None, spent: float = 0.0, unknown: bool = False) -> None:
        self.limit = limit
        self.spent = spent  # DB에 쌓인 이전 실행 비용에서 시작한다
        self.unknown = unknown and limit is not None

    def precheck(self, price: PriceSettings | None, calls: Sequence[tuple[int, int]]) -> None:
        """호출 묶음의 예상 비용(입력·출력 토큰 추정)이 남은 상한을 넘으면 호출 전에 멈춘다."""
        if self.limit is None:
            return
        costs = [token_cost(price, i, o) for i, o in calls]
        if any(c is None for c in costs):
            raise BudgetStop("단가를 몰라 상한을 지킬 수 없다(단가 환경 변수를 설정한다)")
        if self.spent + sum(c for c in costs if c is not None) > self.limit:
            raise BudgetStop(f"다음 호출의 예상 비용이 누적 상한 {self.limit}달러를 넘는다")
        self.check()

    def check(self) -> None:
        if self.unknown:
            raise BudgetStop("비용을 알 수 없어 상한을 지킬 수 없다(단가 환경 변수를 설정한다)")
        if self.limit is not None and self.spent >= self.limit:
            raise BudgetStop(f"누적 비용 상한 {self.limit}달러에 도달했다")

    def add(self, spend: Spend, cost: CallCost) -> None:
        spend.add(cost)
        if cost.cost_usd is not None:
            self.spent += cost.cost_usd
        else:
            self.unknown = self.limit is not None


async def extract_mail(
    llm: LLM, message: MailMessage, price: PriceSettings | None
) -> tuple[Extraction | None, CallCost]:
    boundary = boundary_for(f"a4-extract:{message.id}")
    system = "\n".join([EXTRACT_PROMPT, "", *data_rule_lines(boundary)])
    response = await llm.complete(
        LLMRequest(
            messages=(
                Message(role="system", content=system),
                Message(
                    role="user",
                    content=wrap_data(render_mail(message), source="mail", boundary=boundary),
                ),
            ),
            purpose="system_under_test",
            max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
            metadata={"step_kind": "a4_extract"},
        )
    )
    result = None
    if not response.tool_calls and response.finish_reason != "length":
        result = parse_extraction(response.text)
    return result, _cost(response, price)


async def summarize(
    llm: LLM, key: str, lines: Sequence[str], price: PriceSettings | None
) -> tuple[str | None, CallCost]:
    boundary = boundary_for(f"a4-summary:{key}")
    system = "\n".join([SUMMARY_PROMPT, "", *data_rule_lines(boundary)])
    response = await llm.complete(
        LLMRequest(
            messages=(
                Message(role="system", content=system),
                Message(
                    role="user",
                    content=wrap_data(
                        "\n".join(lines), source="graph_relations", boundary=boundary
                    ),
                ),
            ),
            purpose="system_under_test",
            max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
            metadata={"step_kind": "a4_summary"},
        )
    )
    text = (response.text or "").strip()
    ok = text and not response.tool_calls and response.finish_reason != "length"
    return (text if ok else None), _cost(response, price)


def graph_digest(
    entities: Sequence[store.EntityRow],
    relations: Sequence[store.RelationRow],
    summaries: Sequence[tuple[str, str]],
    embedding_model: str = "",
) -> str:
    """구축 결과 해시. 개체, 관계(출처 메일·주어·관계·객체), 커뮤니티(키·요약 해시), 임베딩 모델."""
    key_of = {e.id: e.name_key for e in entities}
    payload = {
        "embedding_model": embedding_model,
        "extract_limits": extract_limits_sha(),
        "summary_limits": summary_limits_sha(),
        "entities": sorted((e.name_key, e.kind) for e in entities),
        # 정렬 키만 None 안전하게 만들고 payload에는 None을 그대로 둔다.
        "relations": [
            list(t)
            for t in sorted(
                (
                    (
                        r.provider_item_id,
                        key_of[r.subject_id],
                        r.predicate,
                        key_of[r.object_id] if r.object_id else None,
                        r.object_value,
                    )
                    for r in relations
                ),
                key=lambda t: (*t[:3], t[3] is None, t[3] or "", t[4] is None, t[4] or ""),
            )
        ],
        "communities": sorted(
            (k, hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]) for k, s in summaries
        ),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_config(model_id: str, embedding_model: str) -> dict[str, Any]:
    return {
        "extract_tag": extract_tag(model_id),
        "summary_tag": summary_tag(model_id, embedding_model),
        "embedding_model": embedding_model,
        "louvain_seed": LOUVAIN_SEED,
    }


async def build_graph(
    *,
    db: AsyncDatabase,
    ws: uuid.UUID,
    messages: Sequence[MailMessage],
    llm: LLM,
    embedder: Embedder,
    ids: Uuid7Generator,
    clock: Clock,
    model_id: str,
    price: PriceSettings | None = None,
    max_cost_usd: float | None = None,
    rebuild: bool = False,
    other_spent_usd: float = 0.0,
) -> BuildReport:
    """색인 워크스페이스 `ws`의 그래프를 만든다. 이미 끝난 그래프가 있으면 재사용한다."""
    started = time.monotonic()
    report = BuildReport()
    e_tag, s_tag = extract_tag(model_id), summary_tag(model_id, embedder.model_id)
    async with db.workspace_session(ws) as s:
        if rebuild:
            await store.reset_graph(s, ws)
        build = await store.get_build(s, ws)
        done = await store.extraction_tags(s, ws)
        com_tags = await store.community_tags(s, ws)
        item_ids = await store.source_item_ids(s, ws)
        embed_models = await store.entity_embedding_models(s, ws)
        prior = totals_from_rows(await store.cost_rows(s, ws))
    if any(m != embedder.model_id for m in embed_models):
        raise GraphConfigConflict(
            f"개체 임베딩이 다른 모델({embed_models})로 저장돼 있다(--rebuild로 다시 만든다)"
        )
    if (
        any(t != e_tag for t in done.values())
        or any(t != s_tag for t in com_tags.values())
        or (
            build is not None
            and (
                build["config"].get("extract_tag") != e_tag
                or build["config"].get("embedding_model") != embedder.model_id
            )
        )
    ):
        raise GraphConfigConflict(
            "그래프의 추출·요약 설정이 현재와 다르다(--rebuild로 다시 만든다)"
        )
    if build is not None:
        report.reused, report.graph_digest = True, build["graph_digest"]
        report.totals = build["stats"]
        return report
    missing = [m.id for m in messages if m.id not in item_ids]
    if missing:
        raise IndexMismatch(f"색인에 없는 메일 {len(missing)}통(예: {missing[0]})")
    unknown_prior = (
        prior["extraction"]["unknown_cost_calls"] + prior["summary"]["unknown_cost_calls"]
    )
    # 상한 대상은 (다른 계정의 DB 누적 + 이 계정의 DB 누적 + 이번 실행 지출)이다.
    guard = _Guard(max_cost_usd, other_spent_usd + prior["build_cost_usd"], unknown_prior > 0)
    guard.check()
    todo = [m for m in sorted(messages, key=lambda m: m.id) if item_ids[m.id] not in done]
    report.skipped_mails = len(messages) - len(todo)
    for window in _windows(todo, BUILD_CONCURRENCY):
        guard.precheck(
            price,
            [
                (
                    estimate_tokens(len(EXTRACT_PROMPT) + len(render_mail(m)) + DRY_WRAPPER_CHARS),
                    EXTRACT_MAX_OUTPUT_TOKENS,  # 보수적으로 최대 출력으로 잡는다
                )
                for m in window
            ],
        )
        results = await asyncio.gather(
            *(extract_mail(llm, m, price) for m in window), return_exceptions=True
        )
        failure: BaseException | None = None
        for message, result in zip(window, results, strict=True):
            if isinstance(result, BaseException):
                failure = failure or result
                continue
            extraction, cost = result
            report.extracted_mails += 1
            report.format_errors += extraction is None
            async with db.workspace_session(ws) as s:
                await store.save_extraction(
                    s, ws, ids, clock.now(), source_id=item_ids[message.id], result=extraction,
                    cost=cost, config_tag=e_tag,
                )  # fmt: skip
            guard.add(report.extraction, cost)  # 저장한 뒤에 센다(결제된 응답을 잃지 않는다)
        guard.check()
        if failure is not None:
            raise failure
    await _embed_entities(db, ws, embedder)
    await _summarize_communities(
        db, ws, llm, embedder, ids, clock, model_id, price, guard, report, s_tag
    )
    await _embed_communities(db, ws, embedder)  # 요약 저장과 분리: 실패해도 요약을 잃지 않는다
    if report.summary_failures:
        report.seconds = time.monotonic() - started
        return report  # 요약이 빠진 그래프는 완료로 표시하지 않는다(다시 실행하면 이어서 한다)
    async with db.workspace_session(ws) as s:
        entities = await store.all_entities(s, ws)
        relations = await store.all_relations(s, ws)
        summaries = await store.community_summaries(s, ws)
        raw = await store.cost_rows(s, ws)
        report.graph_digest = graph_digest(entities, relations, summaries, embedder.model_id)
        report.totals = {
            "entities": len(entities),
            "relations": len(relations),
            "communities": len(summaries),
            **totals_from_rows(raw),
        }
        await store.save_build(
            s, ws, report.graph_digest, build_config(model_id, embedder.model_id),
            report.totals, clock.now(),
        )  # fmt: skip
    report.seconds = time.monotonic() - started
    return report


async def _embed_entities(db: AsyncDatabase, ws: uuid.UUID, embedder: Embedder) -> None:
    async with db.workspace_session(ws) as s:
        pending = await store.entities_without_embedding(s, ws)
    for batch in _windows(pending, EMBED_BATCH_SIZE):
        vectors = await embedder.embed([e.name for e in batch])
        async with db.workspace_session(ws) as s:
            await store.set_entity_embeddings(s, ws, batch, vectors, embedder.model_id)


async def _embed_communities(db: AsyncDatabase, ws: uuid.UUID, embedder: Embedder) -> None:
    async with db.workspace_session(ws) as s:
        pending = await store.communities_without_embedding(s, ws)
    for batch in _windows(pending, EMBED_BATCH_SIZE):
        vectors = await embedder.embed([summary for _, summary in batch])
        async with db.workspace_session(ws) as s:
            await store.set_community_embeddings(s, ws, batch, vectors, embedder.model_id)


async def _summarize_communities(
    db: AsyncDatabase,
    ws: uuid.UUID,
    llm: LLM,
    embedder: Embedder,
    ids: Uuid7Generator,
    clock: Clock,
    model_id: str,
    price: PriceSettings | None,
    guard: _Guard,
    report: BuildReport,
    s_tag: str,
) -> None:
    async with db.workspace_session(ws) as s:
        entities = await store.all_entities(s, ws)
        relations = await store.all_relations(s, ws)
        existing = set(await store.community_tags(s, ws))
    key_to_id = {e.name_key: e.id for e in entities}
    id_to_key = {e.id: e.name_key for e in entities}
    edges = [(id_to_key[r.subject_id], id_to_key[r.object_id]) for r in relations if r.object_id]
    groups = [
        g
        for g in detect_communities([e.name_key for e in entities], edges)
        if len(g) >= MIN_COMMUNITY_ENTITIES
    ]
    todo = [(community_key(g), g) for g in groups if community_key(g) not in existing]
    for window in _windows(todo, BUILD_CONCURRENCY):
        prepared = []
        for key, group in window:
            members = {key_to_id[k] for k in group}
            all_rels = sorted(
                (r for r in relations if r.subject_id in members),
                key=lambda r: (id_to_key[r.subject_id], r.predicate, r.object_name or "", r.id),
            )
            # 출처 메일은 자르기 전 전체 관계에서 모은다. 프롬프트에만 잘린 목록을 쓴다.
            lines = [
                f"{r.subject_name} | {r.predicate} | {r.object_name}"
                for r in all_rels[:MAX_SUMMARY_RELATIONS]
            ]
            prepared.append((key, members, sorted({r.source_item_id for r in all_rels}), lines))
        guard.precheck(
            price,
            [
                (
                    estimate_tokens(len(SUMMARY_PROMPT) + sum(map(len, lines)) + DRY_WRAPPER_CHARS),
                    SUMMARY_MAX_OUTPUT_TOKENS,
                )
                for _, _, _, lines in prepared
            ],
        )
        results = await asyncio.gather(
            *(summarize(llm, key, lines, price) for key, _, _, lines in prepared),
            return_exceptions=True,
        )
        failure: BaseException | None = None
        done_rows = []
        for item, result in zip(prepared, results, strict=True):
            if isinstance(result, BaseException):
                failure = failure or result
                continue
            summary, cost = result
            if summary is None or not item[2]:
                report.summary_failures += 1
            else:
                done_rows.append((item, summary, cost))
            guard.add(report.summary, cost)
        async with db.workspace_session(ws) as s:
            for (key, members, source_ids, _), summary, cost in done_rows:
                await store.save_community(
                    s, ws, ids, clock.now(), key=key, entity_ids=sorted(members),
                    source_ids=source_ids, summary=summary, config_tag=s_tag, cost=cost,
                )  # fmt: skip
        guard.check()  # 응답을 저장한 뒤에 상한을 검사한다
        if failure is not None:
            raise failure


def totals_from_rows(raw: dict[str, Any]) -> dict[str, Any]:
    """DB 기준 구축 합계(재개 실행을 모두 합친 값)와 메일 1통 추가 시 갱신 비용(진단 5)."""
    ext, com = Spend(), Spend()
    for r in raw["extractions"]:
        ext.add(CallCost(r.input_tokens, r.output_tokens, _num(r.cost_usd), r.latency_ms))
    for r in raw["communities"]:
        com.add(CallCost(r.input_tokens, r.output_tokens, _num(r.cost_usd), r.latency_ms))
    return {
        "extraction": ext.to_dict(),
        "summary": com.to_dict(),
        "build_cost_usd": round(ext.cost_usd + com.cost_usd, 8),
        "build_llm_seconds": round((ext.latency_ms + com.latency_ms) / 1000, 3),
        "format_errors": sum(1 for r in raw["extractions"] if r.status == "format_error"),
        "update_cost": update_cost(raw),
    }


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def update_cost(raw: dict[str, Any]) -> dict[str, Any]:
    """메일 1통을 더했을 때 드는 비용: 그 메일 재추출 1회 + 그 메일이 출처인 커뮤니티 재요약.

    메일마다 (추출 비용 + 그 메일이 출처인 커뮤니티 요약 비용의 합)을 계산해 평균·p50·p95·최대를
    낸다. Louvain 재실행과 개체 임베딩 비용은 넣지 않는다(작다). 비용을 모르는 호출은 비용 합에서
    빠지고 `unknown_cost_mails`로 센다.
    """
    per_mail: dict[Any, dict[str, float]] = {}
    for r in raw["extractions"]:
        per_mail[r.source_item_id] = {
            "usd": _num(r.cost_usd) or 0.0,
            "tokens": float(r.input_tokens + r.output_tokens),
            "calls": 1.0,
            "unknown": float(r.cost_usd is None),
        }
    for r in raw["communities"]:
        for sid in r.source_item_ids:
            m = per_mail.get(sid)
            if m is None:
                continue
            m["usd"] += _num(r.cost_usd) or 0.0
            m["tokens"] += r.input_tokens + r.output_tokens
            m["calls"] += 1
            m["unknown"] += r.cost_usd is None
    if not per_mail:
        return {"mails": 0}
    out: dict[str, Any] = {"mails": len(per_mail)}
    for key in ("usd", "tokens", "calls"):
        values = [m[key] for m in per_mail.values()]
        out[key] = {
            "mean": round(sum(values) / len(values), 8),
            "p50": round(percentile(values, 0.5) or 0.0, 8),
            "p95": round(percentile(values, 0.95) or 0.0, 8),
            "max": round(max(values), 8),
        }
    out["unknown_cost_mails"] = sum(1 for m in per_mail.values() if m["unknown"])
    return out


def estimate_tokens(chars: int) -> int:
    from orbit.evalrunner.retrieval.embed_cache import EST_TOKENS_PER_CHAR  # noqa: PLC0415

    return math.ceil(chars * EST_TOKENS_PER_CHAR)


DRY_OUTPUT_TOKENS_PER_MAIL = 300  # 가정: 개체·관계 JSON 출력 길이(실측 전 값)
DRY_SUMMARY_OUTPUT_TOKENS = 200  # 가정: 2~4문장 요약
DRY_CHARS_PER_RELATION_LINE = 40  # 가정: `주어 | 관계 | 객체` 한 줄 길이
DRY_WRAPPER_CHARS = 300  # 경계 문구·데이터 규칙 등 고정 오버헤드


def dry_run_estimate(
    messages: Sequence[MailMessage],
    gold_edges: Sequence[tuple[str, str]],
    price: PriceSettings | None,
) -> dict[str, Any]:
    """LLM 호출 없이 구축 비용을 추정한다(진단 5 사전 추정).

    - 추출: 메일마다 1회. 입력 토큰은 프롬프트와 메일 렌더링의 글자 수(`estimate_tokens`)로,
      출력 토큰은 가정값으로 센다.
    - 요약: 정답 그래프의 개체-개체 간선에 Louvain을 돌려 개체 2개 이상인 커뮤니티 수를 센다.
      실제 추출은 정답보다 개체가 많아 커뮤니티가 이보다 많을 가능성이 크다(하한). 그래서
      요약 1회 비용과 100회당 비용을 함께 낸다.
    """
    in_tokens = sum(
        estimate_tokens(len(EXTRACT_PROMPT) + len(render_mail(m)) + DRY_WRAPPER_CHARS)
        for m in messages
    )
    out_tokens = DRY_OUTPUT_TOKENS_PER_MAIL * len(messages)
    nodes = sorted({k for e in gold_edges for k in e})
    groups = [g for g in detect_communities(nodes, gold_edges) if len(g) >= MIN_COMMUNITY_ENTITIES]
    per_in = estimate_tokens(
        len(SUMMARY_PROMPT)
        + DRY_WRAPPER_CHARS
        + MAX_SUMMARY_RELATIONS * DRY_CHARS_PER_RELATION_LINE
    )
    # 입력은 관계 수가 상한(60줄)보다 적은 커뮤니티가 많아 상한 기준 = 윗값이다
    ext_cost = token_cost(price, in_tokens, out_tokens)
    sum_cost = token_cost(price, per_in * len(groups), DRY_SUMMARY_OUTPUT_TOKENS * len(groups))
    one = token_cost(price, per_in, DRY_SUMMARY_OUTPUT_TOKENS)
    # 사전 검사는 출력을 최대값으로 잡는다. 같은 입력 추정으로 그 비용을 함께 낸다.
    ext_max = token_cost(price, in_tokens, EXTRACT_MAX_OUTPUT_TOKENS * len(messages))
    sum_max = token_cost(price, per_in * len(groups), SUMMARY_MAX_OUTPUT_TOKENS * len(groups))
    return {
        "mails": len(messages),
        "extraction": {
            "calls": len(messages),
            "input_tokens": in_tokens,
            "output_tokens_assumed": out_tokens,
            "cost_usd": ext_cost,
            "output_tokens_max": EXTRACT_MAX_OUTPUT_TOKENS * len(messages),
            "cost_usd_at_max_output": ext_max,
        },
        "summary": {
            "calls_from_gold_graph_lower_bound": len(groups),
            "input_tokens_per_call_upper": per_in,
            "output_tokens_assumed_per_call": DRY_SUMMARY_OUTPUT_TOKENS,
            "cost_usd": sum_cost,
            "cost_usd_per_call": one,
            "cost_usd_per_100_calls": None if one is None else round(one * 100, 8),
            "output_tokens_max_per_call": SUMMARY_MAX_OUTPUT_TOKENS,
            "cost_usd_at_max_output": sum_max,
        },
        "total_cost_usd": None
        if ext_cost is None or sum_cost is None
        else round(ext_cost + sum_cost, 8),
        "total_cost_usd_at_max_output": None
        if ext_max is None or sum_max is None
        else round(ext_max + sum_max, 8),
        "price_known": ext_cost is not None,
    }


async def recorded_cost(db: AsyncDatabase, workspaces: Sequence[uuid.UUID]) -> tuple[float, bool]:
    """워크스페이스들에 이미 기록된 추출·요약 비용 합과, 비용을 모르는 호출이 있는지."""
    total, unknown = 0.0, False
    for ws in workspaces:
        async with db.workspace_session(ws) as s:
            t = totals_from_rows(await store.cost_rows(s, ws))
        total += t["build_cost_usd"]
        unknown = (
            unknown
            or t["extraction"]["unknown_cost_calls"] + t["summary"]["unknown_cost_calls"] > 0
        )
    return total, unknown


async def load_graph_digests(
    db: AsyncDatabase, workspaces: Mapping[str, uuid.UUID], embedding_model: str | None = None
) -> dict[str, str]:
    """계정별 완료된 그래프의 지문. 구축이 끝나지 않은 계정이 있으면 `GraphNotBuilt`.

    `embedding_model`을 주면 그래프를 만든 임베딩 모델과 다를 때 `GraphConfigConflict`다
    (질의 벡터와 그래프 벡터가 서로 다른 공간이 되는 것을 실행 전에 막는다).
    """
    out: dict[str, str] = {}
    for account, ws in sorted(workspaces.items()):
        async with db.workspace_session(ws) as s:
            build = await store.get_build(s, ws)
        if build is None:
            raise GraphNotBuilt(f"그래프가 없다: {account} (graph build를 먼저 실행한다)")
        built_with = build["config"].get("embedding_model")
        if embedding_model is not None and built_with != embedding_model:
            raise GraphConfigConflict(
                f"그래프({account}) 임베딩 모델 {built_with} != 질의 임베딩 모델 {embedding_model}"
            )
        out[account] = str(build["graph_digest"])
    return out


class GraphNotBuilt(RuntimeError):  # noqa: N818  도메인 용어
    """그래프 구축이 끝나지 않은 계정이 있다."""
