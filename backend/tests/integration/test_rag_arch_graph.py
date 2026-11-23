"""`rag_arch` A4 GraphRAG 통합 테스트 (ADR-0036). 데이터는 모두 합성 값이다.

실제 PostgreSQL(`orbit_test`)에 색인과 그래프를 만든다. 임베더는 `FakeEmbedder`, LLM은 결정적 가짜
(요청 내용으로 답을 고른다)와 `ScriptedLLM`이다. 유료 호출은 없다.
"""

import json
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from integration import test_runflow_eval_index as _base
from integration.test_rag_arch import (
    MAIL_COUNT,
    SUBMIT,
    FakeReranker,
    note_count,
    world_with_mails,
)
from integration.test_runflow_eval_index import BASE, Env, Factory, add_mail, task
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.core.ids import Uuid7Generator
from orbit.evaldata.schema import Turn
from orbit.evalrunner.episode import Budget
from orbit.evalrunner.rag_arch import local_rank as lr
from orbit.evalrunner.rag_arch.answer_norm import RAG_ANSWER_NORM_VERSION
from orbit.evalrunner.rag_arch.graph import build as gb
from orbit.evalrunner.rag_arch.graph import quality as gq
from orbit.evalrunner.rag_arch.graph import store
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.runflow_env import RunflowEnvironment
from orbit.evalrunner.runflow_index import EvalIndexProvider
from orbit.evalrunner.runner import ExperimentSummary, run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import Dataset, load_dataset
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage
from orbit.mailindex.embedding import FakeEmbedder

ix = _base.ix
ACCOUNT = "acct-alice"
MODEL = "fake-graph-model"
NOW = FixedClock(worldmail.INDEX_NOW)
SUMMARY_TEXT = "SYNTH-SUMMARY 김민수와 가나컴퍼니 묶음"
EMPTY: dict[str, list[Any]] = {"entities": [], "relations": []}


def _extraction(i: int) -> str | None:
    """합성 메일 i의 추출 결과(JSON 문자열). None이면 형식이 깨진 응답."""
    if i in (0, 1, 2):
        return json.dumps(
            {
                "entities": [
                    {"name": "김민수님" if i == 0 else "김민수", "type": "person"},
                    {"name": "가나 컴퍼니", "type": "company"},
                ],
                "relations": [
                    {"subject": "김민수", "predicate": "works_at", "object": "가나컴퍼니"}
                ],
            },
            ensure_ascii=False,
        )
    if i in (3, 4):
        return json.dumps(
            {
                "entities": [
                    {"name": "다라프로젝트", "type": "project"},
                    {"name": "라마문서", "type": "document"},
                ],
                "relations": [
                    {"subject": "다라프로젝트", "predicate": "uses", "object": "라마문서"},
                    {"subject": "다라프로젝트", "predicate": "deadline", "object": "2026-12-01"},
                ],
            },
            ensure_ascii=False,
        )
    if i == 5:
        return None
    return json.dumps(EMPTY)


class KeyedLLM:
    """요청 종류(`step_kind`)와 메일 제목으로 답을 고르는 결정적 가짜 LLM."""

    def __init__(self, cost: float | None = None) -> None:
        self.cost = cost
        self.extract_calls = 0
        self.summary_calls = 0
        self.prompts: list[str] = []
        self.summary_prompts: list[str] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kind = request.metadata.get("step_kind")
        user = str(request.messages[-1].content)
        self.prompts.append(user)
        if kind == "a4_extract":
            self.extract_calls += 1
            out = EMPTY_JSON
            for i in range(MAIL_COUNT):
                if f"SYNTH-RAG-{i} " in user:
                    out = _extraction(i) or "깨진 응답"
            body: str | None = out
        else:
            self.summary_calls += 1
            self.summary_prompts.append(user)
            body = SUMMARY_TEXT
        return LLMResponse(
            text=body,
            tool_calls=(),
            finish_reason="stop",
            usage=LLMUsage(100, 20),
            cost_usd=self.cost,
            latency_ms=3,
            model_id=MODEL,
            provider=None,
        )


EMPTY_JSON = json.dumps(EMPTY)


async def build(
    ix: Env,
    provider: EvalIndexProvider,
    world: dict[str, Any],
    llm: KeyedLLM,
    **kw: Any,
) -> gb.BuildReport:
    messages = worldmail.mailbox_messages(SimWorld.load(world, NOW), ACCOUNT)
    return await gb.build_graph(
        db=ix.db,
        ws=provider.handles[ACCOUNT].workspace_id,
        messages=messages,
        llm=llm,
        embedder=FakeEmbedder(),
        ids=Uuid7Generator(NOW),
        clock=NOW,
        model_id=MODEL,
        **{"price": gb.PriceSettings(), **kw},
    )


@pytest.fixture
async def graph(ix: Env) -> tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport]:
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT, "acct-bob"])
    report = await build(ix, provider, world, KeyedLLM(cost=0.001))
    return ix, provider, world, report


async def test_build_merges_entities_records_failures_and_is_reusable(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, report = graph
    assert report.reused is False and report.format_errors == 1 and report.graph_digest
    ws = provider.handles[ACCOUNT].workspace_id
    async with ix.db.workspace_session(ws) as s:
        entities = {e.name_key: e for e in await store.all_entities(s, ws)}
        counts = await store.graph_counts(s, ws)
        mails = await store.entity_mails(s, ws)
        raw = await store.cost_rows(s, ws)
    assert {"김민수", "가나컴퍼니", "다라프로젝트", "라마문서"} <= set(entities)
    assert entities["김민수"].name == "김민수님"  # 먼저 나온 표시 이름이 남는다
    merged = next(m for m in mails if m[0] == "김민수님")
    assert len(merged[2]) == 3  # 세 통의 메일이 한 개체로 합쳐졌다
    assert counts[1] >= 2  # 커뮤니티(김민수-가나컴퍼니, 다라프로젝트-라마문서)
    assert [r.status for r in raw["extractions"]].count("format_error") == 1
    totals = report.totals
    assert totals["format_errors"] == 1 and totals["update_cost"]["mails"] > 0
    assert totals["build_cost_usd"] > 0
    # 같은 설정으로 다시 부르면 LLM을 부르지 않고 같은 지문을 낸다
    again_llm = KeyedLLM()
    again = await build(ix, provider, world, again_llm)
    assert again.reused and again.graph_digest == report.graph_digest
    assert again_llm.extract_calls == again_llm.summary_calls == 0
    with pytest.raises(gb.GraphConfigConflict):  # 모델이 다르면 섞지 않는다
        await gb.build_graph(
            db=ix.db, ws=ws, messages=[], llm=again_llm, embedder=FakeEmbedder(),
            ids=Uuid7Generator(NOW), clock=NOW, model_id="other-model",
        )  # fmt: skip


async def test_build_stops_at_cost_cap_and_resumes_without_recalling_done_mails(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT])
    first = KeyedLLM(cost=0.01)
    priced = gb.PriceSettings(llm_price_input_per_mtok=0.1, llm_price_output_per_mtok=0.5)
    with pytest.raises(gb.BudgetStop):
        await build(ix, provider, world, first, max_cost_usd=0.015, price=priced)
    assert first.extract_calls > 0
    ws = provider.handles[ACCOUNT].workspace_id
    async with ix.db.workspace_session(ws) as s:  # 결제된 응답은 상한에 걸려도 모두 저장돼 있다
        saved = (await s.execute(text("SELECT count(*) FROM mail_graph_extraction"))).scalar_one()
    assert saved == first.extract_calls
    second = KeyedLLM(cost=0.01)
    report = await build(ix, provider, world, second)
    total = len(worldmail.mailbox_messages(SimWorld.load(world, NOW), ACCOUNT))
    assert first.extract_calls < total  # 첫 창만 처리하고 멈췄다
    # 끝난 메일(형식 실패 포함)은 다시 부르지 않는다: 두 실행의 호출 합이 메일 수와 같다
    assert first.extract_calls + second.extract_calls == total
    assert report.skipped_mails == first.extract_calls and report.graph_digest
    assert report.totals["format_errors"] == 1


async def test_build_refuses_before_calling_when_cap_is_set_but_price_unknown(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT])
    llm = KeyedLLM()
    with pytest.raises(gb.BudgetStop):
        await build(ix, provider, world, llm, max_cost_usd=1.0)  # 단가 없음
    assert llm.extract_calls == 0


async def test_build_stops_before_a_window_whose_expected_cost_exceeds_cap(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT])
    llm = KeyedLLM()
    priced = gb.PriceSettings(llm_price_input_per_mtok=1000.0, llm_price_output_per_mtok=1000.0)
    with pytest.raises(gb.BudgetStop):
        await build(ix, provider, world, llm, max_cost_usd=0.001, price=priced)
    assert llm.extract_calls == 0  # 예상 비용이 상한을 넘으니 호출하지 않았다


async def test_build_conflicts_when_embedding_model_differs(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, _ = graph
    ws = provider.handles[ACCOUNT].workspace_id
    with pytest.raises(gb.GraphConfigConflict):
        await gb.build_graph(
            db=ix.db, ws=ws, messages=[], llm=KeyedLLM(), embedder=FakeEmbedder("other-embed"),
            ids=Uuid7Generator(NOW), clock=NOW, model_id=MODEL,
        )  # fmt: skip
    assert gb.graph_digest([], [], [], "a") != gb.graph_digest([], [], [], "b")


async def test_community_sources_come_from_all_relations_before_truncation(
    ix: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "MAX_SUMMARY_RELATIONS", 1)
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT])
    llm = KeyedLLM()
    await build(ix, provider, world, llm)
    assert llm.summary_prompts  # 요약 프롬프트에는 관계가 상한(1줄)까지만 들어간다
    for prompt in llm.summary_prompts:
        assert sum(1 for ln in prompt.splitlines() if ln.count(" | ") == 2) == 1
    ws = provider.handles[ACCOUNT].workspace_id
    async with ix.db.workspace_session(ws) as s:
        sizes = (
            (await s.execute(text("SELECT cardinality(source_item_ids) FROM mail_graph_community")))
            .scalars()
            .all()
        )
    assert max(sizes) >= 3  # 관계를 1개로 잘라도 출처 메일은 세 통(김민수 묶음)이다


async def test_one_char_entity_names_are_never_seeds(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, _, _ = graph
    ws = provider.handles[ACCOUNT].workspace_id
    async with ix.db.workspace_session(ws) as s:
        await s.execute(
            text(
                "INSERT INTO mail_graph_entity (id, workspace_id, name_key, name, kind,"
                " source_item_ids, created_at) VALUES (gen_random_uuid(), :ws, '가', '가',"
                " 'person', ARRAY[gen_random_uuid()], now())"
            ),
            {"ws": ws},
        )
        found = await store.match_entities_by_name(s, ws, "가나컴퍼니와김민수", 8)
    keys = [e.name_key for e in found]
    assert "가" not in keys and "가나컴퍼니" in keys and "김민수" in keys


async def test_rerun_applies_cap_to_cost_already_in_db(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT])
    priced = gb.PriceSettings(llm_price_input_per_mtok=0.1, llm_price_output_per_mtok=0.5)
    with pytest.raises(gb.BudgetStop):
        await build(ix, provider, world, KeyedLLM(cost=0.01), max_cost_usd=0.015, price=priced)
    again = KeyedLLM(cost=0.01)
    with pytest.raises(gb.BudgetStop):  # 이전 실행의 비용이 이미 상한을 넘었다
        await build(ix, provider, world, again, max_cost_usd=0.015, price=priced)
    assert again.extract_calls == 0 and again.summary_calls == 0


async def test_build_stops_before_summaries_when_expected_summary_cost_exceeds_cap(
    ix: Env,
) -> None:
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT])
    first = KeyedLLM(cost=1.0)
    priced = gb.PriceSettings(llm_price_input_per_mtok=0.0, llm_price_output_per_mtok=1000.0)
    total = len(worldmail.mailbox_messages(SimWorld.load(world, NOW), ACCOUNT))
    assert total * 1.024 <= total + 0.4  # 추출 사전 검사(메일당 1.024)는 통과해야 한다
    with pytest.raises(gb.BudgetStop):  # 추출 total회 = total, 요약 예상 0.8 > 남은 0.4
        await build(ix, provider, world, first, max_cost_usd=total + 0.4, price=priced)
    assert first.extract_calls == total and first.summary_calls == 0
    second = KeyedLLM(cost=0.0)
    report = await build(ix, provider, world, second)
    assert second.extract_calls == 0 and second.summary_calls >= 2 and report.graph_digest


class FailingCommunityEmbedder(FakeEmbedder):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if any(t.startswith("SYNTH-SUMMARY") for t in texts):
            raise RuntimeError("synthetic embed failure")
        return await super().embed(texts)


async def test_summaries_survive_embedding_failure_and_resume_embeds_only_missing(
    ix: Env,
) -> None:
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT])
    ws = provider.handles[ACCOUNT].workspace_id
    first = KeyedLLM(cost=0.001)
    messages = worldmail.mailbox_messages(SimWorld.load(world, NOW), ACCOUNT)
    with pytest.raises(RuntimeError, match="synthetic embed failure"):
        await gb.build_graph(
            db=ix.db, ws=ws, messages=messages, llm=first, embedder=FailingCommunityEmbedder(),
            ids=Uuid7Generator(NOW), clock=NOW, model_id=MODEL, price=gb.PriceSettings(),
        )  # fmt: skip
    assert first.summary_calls >= 2
    async with ix.db.workspace_session(ws) as s:
        saved = (await s.execute(text("SELECT count(*) FROM mail_graph_community"))).scalar_one()
        missing = (
            await s.execute(
                text("SELECT count(*) FROM mail_graph_community WHERE summary_embedding IS NULL")
            )
        ).scalar_one()
        probe = (await FakeEmbedder().embed(["김민수"]))[0]
        assert await store.nearest_communities(s, ws, probe, 5) == []  # 임베딩 없는 요약은 제외
    assert saved == missing == first.summary_calls  # 결제한 요약이 모두 남았다
    second = KeyedLLM()
    report = await build(ix, provider, world, second)
    assert second.extract_calls == second.summary_calls == 0  # 요약을 다시 부르지 않는다
    assert report.graph_digest
    async with ix.db.workspace_session(ws) as s:
        left = (
            await s.execute(
                text("SELECT count(*) FROM mail_graph_community WHERE summary_embedding IS NULL")
            )
        ).scalar_one()
        assert left == 0 and len(await store.nearest_communities(s, ws, probe, 5)) >= 1


async def test_build_rejects_mixed_entity_embedding_models(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, _ = graph
    ws = provider.handles[ACCOUNT].workspace_id
    async with ix.db.workspace_session(ws) as s:  # 구축이 중단된 상태(개체 임베딩만 남음)를 만든다
        await s.execute(text("DELETE FROM mail_graph_build WHERE workspace_id = :w"), {"w": ws})
        await s.execute(text("DELETE FROM mail_graph_community WHERE workspace_id = :w"), {"w": ws})
    llm = KeyedLLM()
    messages = worldmail.mailbox_messages(SimWorld.load(world, NOW), ACCOUNT)
    with pytest.raises(gb.GraphConfigConflict, match="임베딩"):
        await gb.build_graph(
            db=ix.db, ws=ws, messages=messages, llm=llm, embedder=FakeEmbedder("other-embed"),
            ids=Uuid7Generator(NOW), clock=NOW, model_id=MODEL, price=gb.PriceSettings(),
        )  # fmt: skip
    assert llm.extract_calls == llm.summary_calls == 0


async def test_quality_matches_year_less_dates_but_not_a_different_year(
    ix: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _extraction

    def year_less(i: int) -> str | None:
        out = original(i)
        return out.replace("2026-12-01", "12월 1일") if out else out

    monkeypatch.setitem(globals(), "_extraction", year_less)
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT])
    await build(ix, provider, world, KeyedLLM())
    ws = provider.handles[ACCOUNT].workspace_id
    async with ix.db.workspace_session(ws) as s:
        ents = await store.entity_mails(s, ws)
        rels = await store.all_relations(s, ws)
    found_rels = [
        gq.FoundRelation(
            r.provider_item_id,
            r.subject_name,
            r.predicate,
            r.object_name or "",
            r.object_id is None,
        )
        for r in rels
    ]

    def gold(date: str) -> list[dict[str, Any]]:
        return [
            {
                "subject": "다라프로젝트", "subject_type": "project", "predicate": "deadline:최종",
                "object": date, "object_type": "date", "sources": ["msg-synth-rag-3"],
            }
        ]  # fmt: skip

    found_ents = [gq.FoundEntity(n, k, m) for n, k, m in ents]
    same = gq.evaluate_quality(
        gold("2026-12-01"), found_ents, found_rels, mails_total=7, format_errors=1
    )
    other = gq.evaluate_quality(
        gold("2026-12-02"), found_ents, found_rels, mails_total=7, format_errors=1
    )
    assert same["relation"]["recall_loose"] == 1.0 and same["relation"]["recall_strict"] == 1.0
    assert other["relation"]["recall_loose"] == 0.0


@pytest.mark.parametrize(
    "name",
    [
        "MAX_ENTITIES_PER_MAIL",
        "MAX_RELATIONS_PER_MAIL",
        "MAX_NAME_CHARS",
        "MAX_PREDICATE_CHARS",
        "EXTRACT_MAX_OUTPUT_TOKENS",
        "MAX_SUMMARY_RELATIONS",
        "SUMMARY_MAX_OUTPUT_TOKENS",
    ],
)
async def test_resume_after_changing_a_limit_constant_conflicts(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ix, provider, world, _ = graph
    monkeypatch.setattr(gb, name, getattr(gb, name) + 1)
    llm = KeyedLLM()
    with pytest.raises(gb.GraphConfigConflict):
        await build(ix, provider, world, llm)
    assert llm.extract_calls == llm.summary_calls == 0


async def test_cap_counts_db_cost_of_every_account_plus_this_run(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, [ACCOUNT, "acct-bob"])
    priced = gb.PriceSettings(llm_price_input_per_mtok=0.1, llm_price_output_per_mtok=0.5)
    alice = provider.handles[ACCOUNT].workspace_id
    bob = provider.handles["acct-bob"].workspace_id
    for account, ws, cap in ((ACCOUNT, alice, 0.015), ("acct-bob", bob, 0.005)):
        with pytest.raises(gb.BudgetStop):  # 두 계정 모두 상한에서 멈춰 비용이 DB에 남는다
            await gb.build_graph(
                db=ix.db, ws=ws, llm=KeyedLLM(cost=0.01), embedder=FakeEmbedder(),
                messages=worldmail.mailbox_messages(SimWorld.load(world, NOW), account),
                ids=Uuid7Generator(NOW), clock=NOW, model_id=MODEL, price=priced,
                max_cost_usd=cap,
            )  # fmt: skip
    alice_cost, _ = await gb.recorded_cost(ix.db, [alice])
    bob_cost, _ = await gb.recorded_cost(ix.db, [bob])
    assert alice_cost > 0 and bob_cost > 0
    total, unknown = await gb.recorded_cost(ix.db, [alice, bob])
    assert unknown is False and total == pytest.approx(alice_cost + bob_cost)
    cap = (alice_cost + bob_cost) * 0.99 + 0.0  # 각 계정 단독으로는 상한 아래, 합은 위
    assert max(alice_cost, bob_cost) < cap < alice_cost + bob_cost
    llm = KeyedLLM(cost=0.01)
    with pytest.raises(gb.BudgetStop):  # 이 계정(bob) 누적 + 다른 계정(alice) 누적 > 상한
        await gb.build_graph(
            db=ix.db, ws=bob, llm=llm, embedder=FakeEmbedder(),
            messages=worldmail.mailbox_messages(SimWorld.load(world, NOW), "acct-bob"),
            ids=Uuid7Generator(NOW), clock=NOW, model_id=MODEL, price=priced,
            max_cost_usd=cap, other_spent_usd=alice_cost,
        )  # fmt: skip
    assert llm.extract_calls == 0
    ok = await gb.build_graph(  # 대조군: 다른 계정 몫을 빼면 같은 상한으로 이어서 끝난다
        db=ix.db, ws=bob, llm=llm, embedder=FakeEmbedder(),
        messages=worldmail.mailbox_messages(SimWorld.load(world, NOW), "acct-bob"),
        ids=Uuid7Generator(NOW), clock=NOW, model_id=MODEL, price=priced, max_cost_usd=cap,
    )  # fmt: skip
    assert ok.graph_digest


async def test_query_path_rejects_graph_built_with_another_embedding_model(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, _, _ = graph
    ws = {ACCOUNT: provider.handles[ACCOUNT].workspace_id}
    assert await gb.load_graph_digests(ix.db, ws, FakeEmbedder().model_id)
    with pytest.raises(gb.GraphConfigConflict, match="임베딩 모델"):
        await gb.load_graph_digests(ix.db, ws, "other-embed")


async def test_graph_rows_are_isolated_by_workspace(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, _, _ = graph
    alice = provider.handles[ACCOUNT].workspace_id
    bob = provider.handles["acct-bob"].workspace_id
    assert alice != bob
    async with ix.db.workspace_session(bob) as s:
        assert await store.graph_counts(s, bob) == (0, 0)
        assert await store.graph_counts(s, alice) == (0, 0)  # 남의 ID를 넘겨도 RLS가 막는다
        assert await store.match_entities_by_name(s, alice, "김민수가나컴퍼니", 5) == []
        embedding = (await FakeEmbedder().embed(["김민수"]))[0]
        assert await store.nearest_entities(s, alice, embedding, 3) == []
        assert await store.nearest_communities(s, alice, embedding, 3) == []
        rows = await s.execute(text("SELECT count(*) FROM mail_graph_relation"))
        assert rows.scalar_one() == 0  # 필터 없이 읽어도 다른 워크스페이스 행은 안 보인다
    async with ix.db.workspace_session(alice) as s:
        assert (await store.graph_counts(s, alice))[0] > 0


ZERO_VECTOR = "[" + ",".join(["0"] * 1536) + "]"
FOREIGN_INSERTS = {
    "mail_graph_entity": (
        "INSERT INTO mail_graph_entity (id, workspace_id, name_key, name, kind, source_item_ids,"
        " created_at) VALUES (gen_random_uuid(), :ws, 'xx', 'xx', 'person',"
        " ARRAY[gen_random_uuid()], now())"
    ),
    "mail_graph_relation": (
        "INSERT INTO mail_graph_relation (id, workspace_id, source_item_id, subject_entity_id,"
        " predicate, object_value) VALUES (gen_random_uuid(), :ws, gen_random_uuid(),"
        " gen_random_uuid(), 'p', 'v')"
    ),
    "mail_graph_community": (
        "INSERT INTO mail_graph_community (id, workspace_id, community_key, entity_ids,"
        " source_item_ids, summary, summary_embedding, embedding_model, config_tag,"
        " input_tokens, output_tokens, latency_ms, created_at)"
        " VALUES (gen_random_uuid(), :ws, 'k', ARRAY[]::uuid[], ARRAY[gen_random_uuid()], 's',"
        " CAST(:emb AS vector), 'm', 't', 0, 0, 0, now())"
    ),
    "mail_graph_extraction": (
        "INSERT INTO mail_graph_extraction (source_item_id, workspace_id, status, config_tag,"
        " entity_count, relation_count, dropped_relations, input_tokens, output_tokens,"
        " latency_ms, created_at)"
        " VALUES (gen_random_uuid(), :ws, 'ok', 't', 0, 0, 0, 0, 0, 0, now())"
    ),
    "mail_graph_build": (
        "INSERT INTO mail_graph_build (workspace_id, graph_digest, config, stats, built_at)"
        " VALUES (:ws, 'd', '{}', '{}', now())"
    ),
}


@pytest.mark.parametrize("table", sorted(FOREIGN_INSERTS))
async def test_graph_tables_hide_and_reject_other_workspace_rows(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport], table: str
) -> None:
    ix, provider, _, _ = graph
    alice = provider.handles[ACCOUNT].workspace_id
    bob = provider.handles["acct-bob"].workspace_id
    count = text(f"SELECT count(*) FROM {table}")  # noqa: S608  고정 테이블 이름
    async with ix.db.workspace_session(alice) as s:
        assert (await s.execute(count)).scalar_one() > 0  # 대조군: 자기 행은 보인다
    async with ix.db.workspace_session(bob) as s:
        assert (await s.execute(count)).scalar_one() == 0  # 읽기: 남의 행이 보이지 않는다
        with pytest.raises(DBAPIError) as info:  # 쓰기: WITH CHECK가 거부한다
            async with s.begin_nested():
                await s.execute(text(FOREIGN_INSERTS[table]), {"ws": alice, "emb": ZERO_VECTOR})
        assert getattr(info.value.orig, "sqlstate", None) == "42501"


def a4_task(question: str, account: str = ACCOUNT) -> Any:
    one = task("t-1", account)
    return one.model_copy(update={"turns": [Turn(role="user", content=question)]})


async def run_a4(
    ix: Env,
    provider: EvalIndexProvider,
    world: dict[str, Any],
    digests: dict[str, str],
    script: Sequence[dict[str, Any]],
    *,
    question: str,
    mode: str = "auto",
    cap: int = 6000,
    account: str = ACCOUNT,
    arch: str = "A4",
    reranker: Any = None,
    budget: Budget | None = None,
) -> tuple[ExperimentSummary, Factory]:
    base = load_dataset(BASE)
    data = Dataset(base.name, base.version, base.manifest, world, (a4_task(question, account),))
    factory = Factory({"t-1": script})
    summary = await run_experiment(
        data,
        split="dev",
        sut_factory=lambda: create_sut(
            "rag_arch",
            rag_arch=arch,
            index_mode="on",
            index=provider,
            context_char_cap=cap,
            graph_digests=digests,
            a4_mode=mode,
            **({"reranker": reranker} if reranker is not None else {}),
        ),
        llm_factory=factory,
        repeats=1,
        budget=budget,
        git_sha="test-sha",
        database_env=RunflowEnvironment(ix.app_url),
    )
    return summary, factory


def assert_no_raw_text(diag: dict[str, Any]) -> None:
    blob = json.dumps(diag, ensure_ascii=False)
    for forbidden in ("SYNTH-BODY", "김민수", "가나", "example.org", "SYNTH-SUMMARY"):
        assert forbidden not in blob


async def test_a4_local_reads_neighbor_mails_with_relation_block(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, report = graph
    digests = {ACCOUNT: report.graph_digest}
    summary, factory = await run_a4(
        ix, provider, world, digests, [{"structured": SUBMIT, "text": "확인했습니다."}],
        question="김민수님은 어느 회사 소속인가요?", mode="local",
    )  # fmt: skip
    row = summary.results[0].to_dict()
    assert row["outcome"]["failure_type"] is None
    diag = row["rag_diag"]
    assert diag["arch"] == "A4" and diag["path"] == "local" and diag["route_forced"] is True
    assert diag["entity_name_matches"] >= 1 and diag["truncated"] is False
    ids = set(diag["context_message_ids"])  # 이름 일치 시드의 세 통 + 임베딩 시드 이웃의 메일
    assert {f"msg-synth-rag-{i}" for i in range(3)} <= ids
    assert ids <= {f"msg-synth-rag-{i}" for i in range(5)}  # 관계가 있는 메일만 후보다
    assert_no_raw_text(diag)
    assert row["usage"]["llm_calls"] == 1  # 경로 강제: 경로 선택 호출 없음
    prompt = "\n".join(str(m.content) for m in factory.made["t-1"].requests[0].messages)
    assert "works_at" in prompt and "[M1]" in prompt and "SYNTH-BODY-0" in prompt
    assert note_count(factory.made["t-1"].requests[0]) == 1
    assert "msg-synth" not in prompt  # 모델에는 핸들만 보인다
    info = row["config"]["sut_config"]
    assert info["graph_digests"] == digests and info["a4_mode"] == "local"
    assert info["answer_norm_version"] == RAG_ANSWER_NORM_VERSION
    assert info["name_match_limit"] == 8 and info["embed_match_limit"] == 3
    assert info["global_sources_per_community"] and info["max_summary_relations"]
    assert diag["relation_lines"] >= 1 and diag["empty_reason"] is None
    assert row["answer_ids"]["messages"][0]["message_id"] in diag["context_message_ids"]


async def test_a4_global_uses_summaries_and_traces_back_to_source_mails(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, report = graph
    digests = {ACCOUNT: report.graph_digest}
    summary, factory = await run_a4(
        ix, provider, world, digests, [{"structured": SUBMIT, "text": "ok"}],
        question="전체 현황을 요약해 주세요", mode="global",
    )  # fmt: skip
    row = summary.results[0].to_dict()
    diag = row["rag_diag"]
    assert diag["path"] == "global" and diag["community_included"] >= 1
    # 커뮤니티 2개의 출처 메일 전부(형식 실패·빈 추출 메일은 출처가 아니다)
    assert set(diag["context_message_ids"]) == {f"msg-synth-rag-{i}" for i in range(5)}
    assert_no_raw_text(diag)
    prompt = str(factory.made["t-1"].requests[0].messages[-1].content)
    assert SUMMARY_TEXT in prompt and "출처:" in prompt and "SYNTH-BODY" not in prompt
    assert note_count(factory.made["t-1"].requests[0]) == 1


async def test_a4_global_cap_truncates_and_records_it(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, report = graph
    summary, _ = await run_a4(
        ix, provider, world, {ACCOUNT: report.graph_digest}, [{"structured": SUBMIT, "text": "ok"}],
        question="전체 현황", mode="global", cap=20,
    )  # fmt: skip
    diag = summary.results[0].to_dict()["rag_diag"]
    assert diag["truncated"] is True and diag["context_chars"] == 20 == diag["char_cap"]


async def test_a4_local_cap_truncates_and_records_it(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, report = graph
    summary, _ = await run_a4(
        ix, provider, world, {ACCOUNT: report.graph_digest}, [{"structured": SUBMIT, "text": "ok"}],
        question="김민수님 소속", mode="local", cap=60,
    )  # fmt: skip
    diag = summary.results[0].to_dict()["rag_diag"]
    assert diag["truncated"] is True and diag["context_chars"] <= 60
    assert diag["relation_lines"] >= 1  # 관계 블록이 글자 몫을 먼저 가져간다
    assert diag["empty_reason"] is None


async def test_a4_global_drops_the_cut_off_source_handle_from_reached(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, report = graph
    cap = len(SUMMARY_TEXT) + len("\n출처: ") + len("M1 M")  # 두 번째 핸들이 "M"에서 잘린다
    summary, _ = await run_a4(
        ix, provider, world, {ACCOUNT: report.graph_digest}, [{"structured": SUBMIT, "text": "ok"}],
        question="전체 현황", mode="global", cap=cap,
    )  # fmt: skip
    diag = summary.results[0].to_dict()["rag_diag"]
    assert diag["truncated"] is True and len(diag["context_message_ids"]) == 1


@pytest.mark.parametrize(
    ("route_text", "path", "fallback"),
    [('{"mode":"global"}', "global", False), ("모르겠다", "local", True)],
)
async def test_a4_auto_routes_with_one_llm_call_and_falls_back_to_local(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
    route_text: str,
    path: str,
    fallback: bool,
) -> None:
    ix, provider, world, report = graph
    summary, factory = await run_a4(
        ix, provider, world, {ACCOUNT: report.graph_digest},
        [{"text": route_text}, {"structured": SUBMIT, "text": "ok"}],
        question="김민수님 소속",
    )  # fmt: skip
    row = summary.results[0].to_dict()
    assert row["rag_diag"]["path"] == path and row["rag_diag"]["route_fallback"] is fallback
    assert row["usage"]["llm_calls"] == 2
    route_req = factory.made["t-1"].requests[0]
    assert [m.content for m in route_req.messages][1] == "김민수님 소속"  # 질문만 본다
    assert route_req.tools == ()
    assert (note_count(route_req), note_count(factory.made["t-1"].requests[1])) == (0, 1)


async def test_a4_fails_loudly_when_graph_is_missing(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, _ = graph
    summary, _ = await run_a4(
        ix, provider, world, {"acct-bob": "x"}, [{"text": "ok"}],
        question="전체 현황", mode="global", account="acct-bob",
    )  # fmt: skip
    assert summary.results[0].to_dict()["outcome"]["failure_type"] is not None


async def test_a4_local_answers_with_empty_context_when_graph_is_empty(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, _ = graph
    summary, _ = await run_a4(
        ix, provider, world, {"acct-bob": "x"}, [{"structured": SUBMIT, "text": "모르겠습니다."}],
        question="김민수님은 어느 회사 소속인가요?", mode="local", account="acct-bob",
    )  # fmt: skip
    row = summary.results[0].to_dict()
    assert row["outcome"]["failure_type"] is None
    diag = row["rag_diag"]
    assert diag["empty_reason"] == "graph_empty" and diag["relation_lines"] == 0
    assert diag["context_message_ids"] == []


async def test_load_graph_digests_requires_finished_graph(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, _, report = graph
    ws = {ACCOUNT: provider.handles[ACCOUNT].workspace_id}
    assert await gb.load_graph_digests(ix.db, ws) == {ACCOUNT: report.graph_digest}
    with pytest.raises(gb.GraphNotBuilt):
        await gb.load_graph_digests(ix.db, {"acct-bob": provider.handles["acct-bob"].workspace_id})


# ---------------------------------------------------------------- A4R·A4RN (ADR-0037)


def mail_number(mail_id: str) -> int:
    return int(mail_id.rsplit("-", 1)[1])


async def test_a4r_local_reorders_candidates_with_reranker_and_keeps_a4_flow(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, report = graph
    reranker = FakeReranker()  # 본문 번호가 클수록 점수가 높다
    summary, factory = await run_a4(
        ix, provider, world, {ACCOUNT: report.graph_digest}, [{"structured": SUBMIT, "text": "ok"}],
        question="김민수님은 어느 회사 소속인가요?", mode="local", arch="A4R", reranker=reranker,
    )  # fmt: skip
    row = summary.results[0].to_dict()
    diag = row["rag_diag"]
    assert diag["arch"] == "A4R" and diag["path"] == "local" and row["usage"]["llm_calls"] == 1
    numbers = [mail_number(m) for m in diag["candidate_message_ids"]]
    assert numbers == sorted(numbers, reverse=True) and len(numbers) >= 3
    assert diag["first_stage_count"] == diag["rerank_input_count"] == len(numbers)
    assert diag["rerank_ms"] >= 0 and len(reranker.calls) == 1
    assert diag["relation_lines"] >= 1 and diag["empty_reason"] is None
    assert_no_raw_text(diag)
    prompt = "\n".join(str(m.content) for m in factory.made["t-1"].requests[0].messages)
    assert "works_at" in prompt
    info = row["config"]["sut_config"]
    assert info["local_rank_version"] and info["graph_rerank_candidates"] == 40
    assert info["graph_digests"] == {ACCOUNT: report.graph_digest}


async def test_a4r_global_path_is_a4_and_never_calls_the_reranker(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, report = graph
    reranker = FakeReranker()
    summary, _ = await run_a4(
        ix, provider, world, {ACCOUNT: report.graph_digest}, [{"structured": SUBMIT, "text": "ok"}],
        question="전체 현황을 요약해 주세요", mode="global", arch="A4R", reranker=reranker,
    )  # fmt: skip
    diag = summary.results[0].to_dict()["rag_diag"]
    assert diag["path"] == "global" and diag["community_included"] >= 1
    assert set(diag["context_message_ids"]) == {f"msg-synth-rag-{i}" for i in range(5)}
    assert reranker.calls == [] and "rerank_ms" not in diag


async def test_a4rn_ranks_whole_mailbox_without_route_call_or_graph(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, world, _ = graph
    reranker = FakeReranker()
    summary, factory = await run_a4(
        ix, provider, world, {}, [{"structured": SUBMIT, "text": "ok"}],
        question="김민수님은 어느 회사 소속인가요?", arch="A4RN", reranker=reranker,
    )  # fmt: skip
    row = summary.results[0].to_dict()
    diag = row["rag_diag"]
    assert row["outcome"]["failure_type"] is None and row["usage"]["llm_calls"] == 1
    assert len(factory.made["t-1"].requests) == 1  # 경로 선택 호출이 없다
    assert "path" not in diag and "relation_lines" not in diag
    assert diag["candidate_message_ids"][0] == f"msg-synth-rag-{MAIL_COUNT - 1}"
    assert diag["first_stage_count"] >= MAIL_COUNT and diag["rerank_input_count"] <= 40
    assert row["config"]["sut_config"]["graph"] == "none"
    assert_no_raw_text(diag)
    prompt = "\n".join(str(m.content) for m in factory.made["t-1"].requests[0].messages)
    assert "works_at" not in prompt and note_count(factory.made["t-1"].requests[0]) == 1


async def test_distance_query_never_mixes_other_workspace_chunks(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    ix, provider, _, _ = graph
    alice = provider.handles[ACCOUNT].workspace_id
    bob = provider.handles["acct-bob"].workspace_id
    probe = (await FakeEmbedder().embed(["견적서 납기"]))[0]
    async with ix.db.workspace_session(bob) as s:
        bob_dist = await lr.mail_distances(s, bob, probe, None)
    async with ix.db.workspace_session(alice) as s:
        alice_dist = await lr.mail_distances(s, alice, probe, None)
        foreign = sorted(set(bob_dist) - set(alice_dist))
        asked = await lr.mail_distances(s, alice, probe, [*foreign, "msg-synth-rag-0"])
        crossed = await lr.mail_distances(s, bob, probe, None)  # 세션 workspace와 다른 필터
    assert foreign and set(alice_dist) >= {f"msg-synth-rag-{i}" for i in range(MAIL_COUNT)}
    assert set(asked) == {"msg-synth-rag-0"}  # 후보에 남의 메일 ID를 넣어도 나오지 않는다
    assert crossed == {}  # RLS와 SQL 필터가 함께 걸려 있다


async def test_distance_sql_filters_workspace_even_without_rls(
    graph: tuple[Env, EvalIndexProvider, dict[str, Any], gb.BuildReport],
) -> None:
    """RLS를 끈 트랜잭션에서도 SQL의 workspace_id 조건이 남의 메일을 막아야 한다.

    테이블에 FORCE RLS가 걸려 있어 소유자 역할도 우회하지 못한다. 그래서 소유자 연결에서 두 테이블의
    RLS를 트랜잭션 안에서만 끄고, 끝나면 롤백한다.
    """
    ix, provider, _, _ = graph
    alice = provider.handles[ACCOUNT].workspace_id
    bob = provider.handles["acct-bob"].workspace_id
    probe = (await FakeEmbedder().embed(["견적서 납기"]))[0]
    engine = create_async_engine(ix.owner_url, pool_size=1, max_overflow=0)
    try:
        async with AsyncSession(engine) as s:
            for table in ("chunk", "source_item"):
                await s.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))  # noqa: S608
            await s.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(alice)}
            )
            # 대조군: RLS가 꺼져 alice 세션에서도 bob의 청크가 보인다
            visible = (
                await s.execute(
                    text("SELECT count(*) FROM chunk WHERE workspace_id = :ws"), {"ws": bob}
                )
            ).scalar_one()
            assert visible > 0
            alice_dist = await lr.mail_distances(s, alice, probe, None)
            bob_dist = await lr.mail_distances(s, bob, probe, None)
            foreign = sorted(set(bob_dist) - set(alice_dist))
            asked = await lr.mail_distances(s, alice, probe, [*foreign, "msg-synth-rag-0"])
            await s.rollback()  # RLS 설정을 되돌린다
    finally:
        await engine.dispose()
    assert "msg-synth-bob" in bob_dist and "msg-synth-bob" not in alice_dist
    assert foreign and set(asked) == {"msg-synth-rag-0"}
    assert not set(alice_dist) & set(foreign)


async def test_a4rn_cuts_to_12_mails_and_40_rerank_inputs_on_a_large_mailbox(ix: Env) -> None:
    world = world_with_mails()
    for i in range(45):  # 색인된 메일이 40통을 넘게 한다
        add_mail(
            world, ACCOUNT, f"msg-synth-extra-{i:02}",
            {"subject": f"SYNTH-EXTRA-{i} 견적서", "body": f"SYNTH-BODY-{100 + i} 납기 안내."},
        )  # fmt: skip
    provider = await ix.build(world, [ACCOUNT])
    # 후보 40통 읽기가 단계(커넥터 호출)로 세어져 기본 한도 50으로는 모자라다(보고 참고)
    summary, _ = await run_a4(
        ix, provider, world, {}, [{"structured": SUBMIT, "text": "ok"}],
        question="견적서 납기를 알려 주세요", arch="A4RN", reranker=FakeReranker(),
        budget=Budget(max_steps=100),
    )  # fmt: skip
    row = summary.results[0].to_dict()
    assert row["outcome"]["failure_type"] is None
    diag = row["rag_diag"]
    assert diag["first_stage_count"] > 40
    assert diag["rerank_input_count"] == min(40, diag["first_stage_count"]) == 40
    assert len(diag["candidate_message_ids"]) == 12
    assert len(set(diag["candidate_message_ids"])) == 12
