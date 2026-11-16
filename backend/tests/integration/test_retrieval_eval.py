"""검색 평가 실행기(ADR-0028) 통합: 색인 만들기·재사용·지문, 방식별 순위, 전체 실행.

데이터는 모두 합성 값이다. 임베더는 `FakeEmbedder`(호출 수를 세는 래퍼)다.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from integration.mailindex_support import (
    EXTRAS,
    WORLD_PATH,
    CountingEmbedder,
    IndexEnv,
    add_inbox,
    mail,
)
from integration.support import WorkspaceFactory
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.db.session import AsyncDatabase
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.embed_cache import CachingEmbedder
from orbit.evalrunner.retrieval.indexer import EvalIndexer, IndexHandle
from orbit.evalrunner.retrieval.methods import METHODS, MailboxSearcher, build_bm25, rank
from orbit.evalrunner.retrieval.queries import MailQuery, QuerySelection
from orbit.evalrunner.retrieval.runner import RunSettings, run_retrieval
from orbit.mailindex.search import search_index
from orbit.mailindex.sync import sync_connection
from orbit.mailindex.tokenize import tokenize

ACCOUNT = "acct-alice"
USER_SUB_PREFIX = "eval-retrieval-"


@dataclass
class Env:
    db: AsyncDatabase
    world: SimWorld
    embedder: CountingEmbedder
    cache: CachingEmbedder
    owner_url: str
    workspaces: WorkspaceFactory

    def indexer(self, world_sha: str = "w1") -> EvalIndexer:
        return EvalIndexer(
            db=self.db,
            owner_url=self.owner_url,
            embedder=self.cache,
            world_sha256=world_sha,
            expected_mails=lambda a: len(worldmail.mailbox_messages(self.world, a)),
            chunk_texts=lambda a: worldmail.mailbox_chunks(self.world, a),
        )

    async def ensure(self, world_sha: str = "w1") -> IndexHandle:
        conns = build_sim_connectors(self.world, [ACCOUNT])
        handle = await self.indexer(world_sha).ensure(
            ACCOUNT, conns.mail[ACCOUNT], self.world.account(ACCOUNT).email
        )
        self.workspaces.created.append(handle.workspace_id)  # 테스트가 끝나면 지운다
        return handle

    async def count(self, handle: IndexHandle, table: str = "source_item") -> int:
        async with self.db.workspace_session(handle.workspace_id) as s:
            sql = f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"  # noqa: S608
            return int((await s.execute(text(sql), {"ws": handle.workspace_id})).scalar_one())


@pytest.fixture
async def rx(
    async_db: AsyncDatabase,
    workspaces: WorkspaceFactory,
    owner_engine: Engine,
    owner_url: str,
    tmp_path: Path,
) -> AsyncIterator[Env]:
    world = SimWorld.load(WORLD_PATH, FixedClock(worldmail.INDEX_NOW))
    state = world.account(ACCOUNT)
    for message in EXTRAS:
        state.messages[message.id] = message
        state.threads.setdefault(message.thread_id, []).append(message.id)
    embedder = CountingEmbedder()
    env = Env(async_db, world, embedder, CachingEmbedder(embedder, tmp_path), owner_url, workspaces)
    yield env
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM app_user WHERE google_sub LIKE :p"), {"p": USER_SUB_PREFIX + "%"}
        )


async def test_builds_index_then_reuses_without_embedding(rx: Env) -> None:
    first = await rx.ensure()
    expected = len(worldmail.mailbox_messages(rx.world, ACCOUNT))
    assert not first.reused and first.indexed_mails == first.expected_mails == expected
    assert first.chunks == len(worldmail.mailbox_chunks(rx.world, ACCOUNT))  # dry-run 계산과 같다
    built = rx.embedder.embedded
    assert built > 0 and rx.cache.misses == built

    again = await rx.ensure()
    assert again.reused and again.workspace_id == first.workspace_id
    assert rx.embedder.embedded == built  # 두 번째 실행은 임베딩 0회
    assert await rx.count(again) == expected


async def test_cache_survives_dropping_the_index(rx: Env) -> None:
    first = await rx.ensure()
    built = rx.embedder.embedded
    async with rx.db.workspace_session(first.workspace_id) as s:  # DB 색인만 비운다
        await s.execute(text("DELETE FROM workspace WHERE id = :ws"), {"ws": first.workspace_id})
    rebuilt = await rx.ensure()
    assert not rebuilt.reused and rx.embedder.embedded == built  # 캐시 적중


async def test_changed_fingerprint_builds_new_index_and_keeps_old(rx: Env) -> None:
    a = await rx.ensure("w1")
    b = await rx.ensure("w2")  # 세계 해시가 다르다
    assert a.fingerprint != b.fingerprint and a.workspace_id != b.workspace_id
    assert not b.reused
    assert await rx.count(a) > 0 and await rx.count(b) > 0  # 지문이 다른 색인은 지우지 않는다


async def test_missing_chunk_row_prevents_reuse(rx: Env) -> None:
    first = await rx.ensure()
    async with rx.db.workspace_session(first.workspace_id) as s:  # chunk 행 하나만 잃는다
        await s.execute(
            text(
                "DELETE FROM chunk WHERE id = (SELECT id FROM chunk"
                " WHERE workspace_id = :ws LIMIT 1)"
            ),
            {"ws": first.workspace_id},
        )
    again = await rx.ensure()
    assert not again.reused  # 메일 수만 맞아도 chunk 수가 다르면 완성된 색인이 아니다


async def test_two_fingerprints_do_not_leak_across_workspaces(rx: Env) -> None:
    a = await rx.ensure("w1")
    extra = mail("m-only-b", "신규 보안 감사 일정", "감사팀 방문은 다음 주 화요일이다")
    state = rx.world.account(ACCOUNT)
    state.messages[extra.id] = extra
    state.threads.setdefault(extra.thread_id, []).append(extra.id)
    b = await rx.ensure("w2")  # 같은 계정, 다른 지문 = 다른 워크스페이스
    assert a.workspace_id != b.workspace_id
    conns = build_sim_connectors(rx.world, [ACCOUNT])

    def searcher(handle: IndexHandle) -> MailboxSearcher:
        return MailboxSearcher(
            account_id=ACCOUNT,
            workspace_id=handle.workspace_id,
            mail=conns.mail[ACCOUNT],
            bm25=build_bm25(rx.world, ACCOUNT),
        )

    for method in ("keyword", "vector", "hybrid"):
        ranked_a = await rank(method, "보안 감사 일정", searcher(a), rx.db, rx.cache)
        ranked_b = await rank(method, "보안 감사 일정", searcher(b), rx.db, rx.cache)
        assert "m-only-b" not in ranked_a, method  # w1 색인에는 없는 메일이다
        if method != "vector":
            assert "m-only-b" in ranked_b, method


async def test_methods_rank_mail_ids(rx: Env) -> None:
    handle = await rx.ensure()
    conns = build_sim_connectors(rx.world, [ACCOUNT])
    searcher = MailboxSearcher(
        account_id=ACCOUNT,
        workspace_id=handle.workspace_id,
        mail=conns.mail[ACCOUNT],
        bm25=build_bm25(rx.world, ACCOUNT),
    )
    ranked = {m: await rank(m, "견적서 납기", searcher, rx.db, rx.cache) for m in METHODS}
    for method in ("keyword", "hybrid", "bm25"):
        assert ranked[method][0] == "m-quote", method
    assert "m-quote" in ranked["vector"][:3]
    assert ranked["gmail_search"] == ["m-quote"]  # Sim 검색: 두 단어가 모두 들어간 메일
    assert all(len(r) == len(set(r)) <= 20 for r in ranked.values())
    # 키워드·벡터 방식은 운영 SQL의 한쪽만 쓴다
    assert "m-lunch" not in ranked["keyword"]
    assert len(ranked["vector"]) > len(ranked["keyword"])  # 벡터는 토큰이 없어도 이웃을 돌려준다


async def test_search_mode_default_is_unchanged_hybrid(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    emb = (await ix.embedder.embed(["견적 납기"]))[0]
    tokens = tokenize("견적 납기")
    async with ix.db.workspace_session(ix.ws) as s:
        default = await search_index(s, ix.ws, embedding=emb, tokens=tokens, limit=5)
        hybrid = await search_index(s, ix.ws, embedding=emb, tokens=tokens, limit=5, mode="hybrid")
        kw = await search_index(s, ix.ws, embedding=emb, tokens=tokens, limit=5, mode="keyword")
        vec = await search_index(s, ix.ws, embedding=emb, tokens=tokens, limit=5, mode="vector")
        none_kw = await search_index(s, ix.ws, embedding=emb, tokens=[], limit=5, mode="keyword")
        with pytest.raises(ValueError):
            await search_index(s, ix.ws, embedding=emb, tokens=tokens, limit=5, mode="x")  # type: ignore[arg-type]
    assert [h.provider_item_id for h in default] == [h.provider_item_id for h in hybrid]
    assert [(h.provider_item_id, h.score) for h in default] == [
        (h.provider_item_id, h.score) for h in hybrid
    ]
    assert {h.provider_item_id for h in kw} <= {"m-quote", "m-price"}
    assert len(vec) > len(kw) and none_kw == []


async def test_first_sync_max_overrides_default(ix: IndexEnv) -> None:
    await add_inbox(ix)
    result = await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid, first_sync_max=2)
    assert result.outcome == "synced" and result.indexed == 2
    assert len(await ix.item_ids()) == 2  # 기본값(2,000)이면 5통이 모두 들어간다(기존 테스트)


async def test_full_run_writes_report_without_text(rx: Env) -> None:
    q = MailQuery(
        query_id="q-1",
        query="견적서 납기",
        form="keyword",
        cluster_id="c-1",
        accounts={"200": ACCOUNT},
        relevance={"200": {"m-quote": 2, "m-price": 1, "m-lunch": 0}},
    )
    selection = QuerySelection((q,), {"selected": 1, "document_answer_excluded": 0})
    settings = RunSettings(
        split="dev", sizes=("200",), methods=METHODS, resamples=50, bootstrap_seed=0,
        experiment_id="e-test", dataset="d", world_sha256="w1", search_sha256="s1",
        git_sha="abc", git_dirty=False, test_runs=None,
    )  # fmt: skip
    result = await run_retrieval(
        settings=settings,
        selection=selection,
        world=rx.world,
        db=rx.db,
        owner_url=rx.owner_url,
        embedder=rx.cache,
        accounts={"200": ACCOUNT},
    )
    rx.workspaces.created.append(uuid.UUID(result["index"]["200"]["workspace_id"]))
    assert result["experiment_id"] == "e-test" and result["code"]["git_sha"] == "abc"
    assert result["config_fingerprint"] and result["index_fingerprints"]["200"]
    assert set(result["summary"]["200"]) == set(METHODS)
    pq = result["per_query"][0]
    assert pq["ranks"]["200"]["bm25"][0] == "m-quote" and pq["metrics"]["200"]["bm25"]["mrr"] == 1.0
    assert result["embedding"]["cache_misses"] > 0 and result["queries"]["selected"] == 1
    assert "견적서" not in str(result["per_query"])  # 질의 원문은 결과에 없다(ID만)
