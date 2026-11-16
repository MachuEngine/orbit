"""검색 평가 실행 (ADR-0028). 색인 확보 -> 질의 임베딩 -> 방식별 순위 -> 지표 -> 결과 딕셔너리."""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.db.session import AsyncDatabase
from orbit.evalrunner.retrieval import metrics, worldmail
from orbit.evalrunner.retrieval.bm25 import K1, B
from orbit.evalrunner.retrieval.embed_cache import CachingEmbedder
from orbit.evalrunner.retrieval.fingerprint import digest
from orbit.evalrunner.retrieval.indexer import EvalIndexer, IndexHandle
from orbit.evalrunner.retrieval.methods import TOP_N, MailboxSearcher, build_bm25, rank
from orbit.evalrunner.retrieval.queries import QuerySelection
from orbit.mailindex.limits import INDEX_CODE_VERSION, INDEX_CONFIG

SCHEMA = "retrieval-eval-v1"


@dataclass(frozen=True)
class RunSettings:
    split: str
    sizes: tuple[str, ...]
    methods: tuple[str, ...]
    resamples: int
    bootstrap_seed: int
    experiment_id: str | None
    dataset: str
    world_sha256: str
    search_sha256: str
    git_sha: str
    git_dirty: bool
    test_runs: int | None  # test 분할이면 이번을 포함한 실행 횟수(ADR-0010)


def config_payload(settings: RunSettings, model_id: str) -> dict[str, Any]:
    """결과의 `config`. 실행 결과에 영향을 주는 값만 담는다(`config_fingerprint`의 입력)."""
    return {
        "schema": SCHEMA,
        "split": settings.split,
        "sizes": list(settings.sizes),
        "methods": list(settings.methods),
        "top_n": TOP_N,
        "embedding_model_id": model_id,
        "bm25": {"k1": K1, "b": B},
        "index_config": dict(INDEX_CONFIG),
        "index_code_version": INDEX_CODE_VERSION,
        "scope": {
            "labels": list(worldmail.SCOPE_LABELS),
            "period_days": worldmail.SCOPE_PERIOD_DAYS,
            "index_now": worldmail.INDEX_NOW.isoformat(),
            "first_sync_max": worldmail.EVAL_FIRST_SYNC_MAX,
        },
        "bootstrap": {"resamples": settings.resamples, "seed": settings.bootstrap_seed},
        "world_sha256": settings.world_sha256,
        "search_sha256": settings.search_sha256,
    }


async def run_retrieval(
    *,
    settings: RunSettings,
    selection: QuerySelection,
    world: SimWorld,
    db: AsyncDatabase,
    owner_url: str,
    embedder: CachingEmbedder,
    accounts: dict[str, str],
) -> dict[str, Any]:
    started = time.monotonic()
    account_ids = [accounts[s] for s in settings.sizes]
    conns = build_sim_connectors(world, account_ids)
    indexer = EvalIndexer(
        db=db,
        owner_url=owner_url,
        embedder=embedder,
        world_sha256=settings.world_sha256,
        expected_mails=lambda a: len(worldmail.mailbox_messages(world, a)),
        chunk_texts=lambda a: worldmail.mailbox_chunks(world, a),
    )
    handles: dict[str, IndexHandle] = {}
    searchers: dict[str, MailboxSearcher] = {}
    for size in settings.sizes:
        account = accounts[size]
        handle = await indexer.ensure(account, conns.mail[account], world.account(account).email)
        handles[size] = handle
        searchers[size] = MailboxSearcher(
            account_id=account,
            workspace_id=handle.workspace_id,
            mail=conns.mail[account],
            bm25=build_bm25(world, account),
        )
    index_seconds = time.monotonic() - started

    if {"vector", "hybrid"} & set(settings.methods) and selection.queries:
        await embedder.embed([q.query for q in selection.queries])  # 질의 임베딩을 일괄로 캐시한다

    per_query: list[dict[str, Any]] = []
    for q in selection.queries:
        ranks: dict[str, dict[str, list[str]]] = {}
        scores: dict[str, dict[str, dict[str, float]]] = {}
        for size in settings.sizes:
            ranks[size] = {}
            scores[size] = {}
            for method in settings.methods:
                ranked = await rank(method, q.query, searchers[size], db, embedder)
                ranks[size][method] = ranked
                scores[size][method] = metrics.query_metrics(q.relevance[size], ranked)
        per_query.append(
            {
                "query_id": q.query_id,
                "form": q.form,
                "cluster_id": q.cluster_id,
                "ranks": ranks,
                "metrics": scores,
            }
        )

    report = metrics.build_report(
        per_query,
        settings.sizes,
        settings.methods,
        resamples=settings.resamples,
        seed=settings.bootstrap_seed,
    )
    config = config_payload(settings, embedder.model_id)
    config_fp = digest(config)
    index_fps = {s: h.fingerprint for s, h in handles.items()}
    return {
        "schema": SCHEMA,
        "experiment_id": settings.experiment_id or f"retrieval-{config_fp[:12]}",
        "config": config,
        "config_fingerprint": config_fp,
        "index_fingerprints": index_fps,
        "code": {"git_sha": settings.git_sha, "git_dirty": settings.git_dirty},
        "dataset": settings.dataset,
        "split": settings.split,
        "test_runs": settings.test_runs,
        "queries": selection.counts,
        "index": {s: h.to_dict() for s, h in handles.items()},
        "embedding": embedder.stats(),
        "gmail_search_errors": {s: dict(searchers[s].errors) for s in settings.sizes},
        "timing": {
            "index_seconds": round(index_seconds, 3),
            "total_seconds": round(time.monotonic() - started, 3),
        },
        **report,
        "per_query": per_query,
    }


def sequence_sizes(value: str) -> tuple[str, ...]:
    sizes = tuple(dict.fromkeys(v.strip() for v in value.split(",") if v.strip()))
    unknown = [s for s in sizes if s not in ("200", "1000", "5000")]
    if unknown or not sizes:
        raise ValueError(f"알 수 없는 메일함 크기: {unknown or value}")
    return sizes


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
