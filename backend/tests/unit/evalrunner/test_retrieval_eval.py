"""검색 평가 실행기(ADR-0028)의 DB가 필요 없는 부분: BM25·캐시·지문·지표·질의 선택·CLI."""

import json
import math
import subprocess  # noqa: S404  git check-ignore만 부른다
from pathlib import Path

import pytest

from orbit.evalrunner.cli import SEAL_FLAG
from orbit.evalrunner.retrieval import fingerprint, metrics, worldmail
from orbit.evalrunner.retrieval import queries as qmod
from orbit.evalrunner.retrieval.bm25 import Bm25Index, query_terms
from orbit.evalrunner.retrieval.cli import UsageError, _eval_urls, _record_test_run, main
from orbit.evalrunner.retrieval.dryrun import dry_run_report
from orbit.evalrunner.retrieval.embed_cache import (
    BudgetExceeded,
    CacheCorruptError,
    CachingEmbedder,
    cache_path,
    read_cached_keys,
    text_key,
)
from orbit.evalrunner.retrieval.methods import METHODS, parse_methods
from orbit.mailindex import limits
from orbit.mailindex.embedding import FakeEmbedder

DATASET = Path(__file__).resolve().parents[4] / "eval" / "datasets" / "orbit-eval" / "v1"
REPO = Path(__file__).resolve().parents[4]


# ---------------------------------------------------------------- BM25


def test_bm25_matches_hand_calculation() -> None:
    docs = {
        "a": ["견적", "견적", "납기"],
        "b": ["견적", "점심"],
        "c": ["점심", "김밥", "우동", "메뉴"],
    }
    index = Bm25Index(docs, k1=1.2, b=0.75)
    n, avgdl = 3, (3 + 2 + 4) / 3

    def idf(df: int) -> float:
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def part(tf: int, dl: int, df: int) -> float:
        return idf(df) * tf * 2.2 / (tf + 1.2 * (1 - 0.75 + 0.75 * dl / avgdl))

    assert index.score("a", ["견적", "납기"]) == pytest.approx(part(2, 3, 2) + part(1, 3, 1))
    assert index.score("b", ["견적", "납기"]) == pytest.approx(part(1, 2, 2))
    result = index.search(["견적", "납기"], 10)
    assert [d for d, _ in result] == ["a", "b"]  # c는 점수 0이라 빠진다
    assert result[0][1] == pytest.approx(part(2, 3, 2) + part(1, 3, 1))


def test_bm25_ties_break_by_id_and_terms_are_unique_normalized() -> None:
    index = Bm25Index({"b": ["가"], "a": ["가"]})
    assert [d for d, _ in index.search(["가"], 5)] == ["a", "b"]
    assert query_terms(["견적!", "견적", "A-B", ""]) == ["견적", "ab"]
    assert Bm25Index({}).search(["가"], 5) == []


# ---------------------------------------------------------------- 임베딩 캐시


async def test_cache_hits_persist_and_skip_inner_calls(tmp_path: Path) -> None:
    calls: list[int] = []

    class Counting(FakeEmbedder):
        async def embed(self, texts):  # type: ignore[no-untyped-def]
            calls.append(len(texts))
            return await super().embed(texts)

    first = CachingEmbedder(Counting(), tmp_path)
    v1 = await first.embed(["견적 납기", "점심 메뉴", "견적 납기"])
    assert (first.misses, first.hits, calls) == (2, 0, [2])  # 같은 텍스트는 한 번만 보낸다
    assert v1[0] == v1[2]

    second = CachingEmbedder(Counting(), tmp_path)  # 새 프로세스처럼 파일에서 읽는다
    v2 = await second.embed(["점심 메뉴", "견적 납기"])
    assert (second.misses, second.hits, calls) == (0, 2, [2])
    # float32로 저장하므로 pgvector에 들어가는 값과 같다
    assert v2[0] == pytest.approx(v1[1], abs=1e-6)
    assert second.cached("견적 납기") and not second.cached("없는 글")
    line = cache_path(tmp_path, "fake-embedding-v1").read_text(encoding="utf-8").splitlines()[0]
    assert set(json.loads(line)) == {"k", "v"} and "견적" not in line  # 원문은 저장하지 않는다
    assert json.loads(line)["k"] in {text_key("견적 납기"), text_key("점심 메뉴")}


async def test_cache_drops_truncated_last_line_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = CachingEmbedder(FakeEmbedder(), tmp_path)
    await first.embed(["가", "나"])
    path = first.path
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"k": "abc", "v": "AAA')  # 쓰다 끊긴 마지막 줄(개행 없음)
    capsys.readouterr()
    second = CachingEmbedder(FakeEmbedder(), tmp_path)
    err = capsys.readouterr().err
    assert len(second) == 2 and err.count("경고") == 1 and len(err.strip().splitlines()) == 1
    await second.embed(["다"])  # 덧붙여도 깨진 꼬리가 이어 붙지 않는다
    third = CachingEmbedder(FakeEmbedder(), tmp_path)
    assert len(third) == 3 and third.cached("다")


def test_cache_middle_line_corruption_is_an_error(tmp_path: Path) -> None:
    path = cache_path(tmp_path, "fake-embedding-v1")
    good = json.dumps({"k": "a", "v": ""})
    path.write_text(f"{good}\n{{broken\n{good}\n", encoding="utf-8")
    with pytest.raises(CacheCorruptError, match="2번째"):
        CachingEmbedder(FakeEmbedder(), tmp_path)
    with pytest.raises(CacheCorruptError):
        read_cached_keys(tmp_path, "fake-embedding-v1")


@pytest.mark.parametrize("bad", ["app", "owner"])
def test_eval_urls_reject_non_eval_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    bad: str,
) -> None:
    ok = "postgresql+psycopg://u:p@localhost:5432/orbit_eval"
    dev = "postgresql+psycopg://u:p@localhost:5432/orbit"
    monkeypatch.setenv("ORBIT_EVAL_DATABASE_URL", dev if bad == "app" else ok)
    monkeypatch.setenv("ORBIT_EVAL_MIGRATION_DATABASE_URL", dev if bad == "owner" else ok)
    with pytest.raises(UsageError, match="orbit_eval"):
        _eval_urls()
    code = main(
        ["run", "--dataset", str(DATASET), "--split", "dev", "--embedder", "fake",
         "--sizes", "200", "--out", str(tmp_path / "o.json"),
         "--embed-cache-dir", str(tmp_path / "c")]
    )  # fmt: skip
    assert code == 2 and "orbit_eval" in capsys.readouterr().err
    monkeypatch.setenv("ORBIT_EVAL_DATABASE_URL", ok)
    monkeypatch.setenv("ORBIT_EVAL_MIGRATION_DATABASE_URL", ok)
    assert _eval_urls() == (ok, ok)


async def test_cache_is_per_model(tmp_path: Path) -> None:
    a = CachingEmbedder(FakeEmbedder("m-a"), tmp_path)
    await a.embed(["글"])
    b = CachingEmbedder(FakeEmbedder("m-b"), tmp_path)
    await b.embed(["글"])
    assert b.misses == 1  # 다른 모델은 같은 텍스트도 다시 임베딩한다


async def test_cache_stops_before_exceeding_budget(tmp_path: Path) -> None:
    emb = CachingEmbedder(FakeEmbedder("text-embedding-3-small"), tmp_path, max_cost_usd=0.00001)
    with pytest.raises(BudgetExceeded):
        await emb.embed(["가" * 1000])  # 추정 1000토큰 = 0.00002달러
    assert emb.misses == 1 and len(emb) == 0  # 호출 전에 멈췄으므로 저장된 것이 없다
    unknown = CachingEmbedder(FakeEmbedder("모르는-모델"), tmp_path, max_cost_usd=1.0)
    with pytest.raises(BudgetExceeded):
        await unknown.embed(["글"])  # 단가를 모르면 상한을 확인할 수 없어 거부한다


# ---------------------------------------------------------------- 색인 지문


def _fp(**kw: object) -> str:
    base: dict[str, object] = {
        "world_sha256": "w1",
        "account_id": "acct-s200",
        "model_id": "text-embedding-3-small",
        "first_sync_max": 10_000,
    }
    return fingerprint.index_fingerprint(**{**base, **kw})  # type: ignore[arg-type]


def test_fingerprint_changes_with_each_input(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _fp()
    assert base == _fp()
    assert len({base, _fp(world_sha256="w2"), _fp(account_id="acct-s1000")}) == 3
    assert _fp(model_id="other") != base and _fp(first_sync_max=2_000) != base
    monkeypatch.setattr(fingerprint, "INDEX_CODE_VERSION", limits.INDEX_CODE_VERSION + 1)
    assert _fp() != base  # 색인 코드 버전
    monkeypatch.undo()
    monkeypatch.setattr(fingerprint, "CHUNK_CHARS", limits.CHUNK_CHARS + 1)
    assert _fp() != base  # 청크 설정
    monkeypatch.undo()
    monkeypatch.setattr(fingerprint, "tokenizer_config", lambda: {"kiwipiepy": "0.0"})
    assert _fp() != base  # 토큰화 설정


def test_derived_ids_follow_fingerprint() -> None:
    fp = _fp()
    assert fingerprint.derived_id(fp, "workspace") == fingerprint.derived_id(fp, "workspace")
    assert fingerprint.derived_id(fp, "workspace") != fingerprint.derived_id(fp, "connection")
    assert fingerprint.derived_id(fp, "workspace") != fingerprint.derived_id(
        _fp(world_sha256="x"), "workspace"
    )


# ---------------------------------------------------------------- 지표


def test_query_metrics_and_report_shapes() -> None:
    rel = {"m1": 2, "m2": 1, "m3": 0}
    got = metrics.query_metrics(rel, ["m3", "m2", "m1"])
    assert got["recall@5"] == 1.0 and got["recall_ge1@5"] == 1.0
    assert got["mrr"] == pytest.approx(1 / 3)
    assert got["recall@5"] == got["recall@20"]
    rows = [
        {
            "query_id": f"q{i}",
            "form": "keyword" if i % 2 else "person_time",
            "cluster_id": f"c{i // 2}",
            "metrics": {
                "200": {
                    "a": metrics.query_metrics(rel, ["m1"]),
                    "b": metrics.query_metrics(rel, ["m3", "m1"]),
                }
            },
        }
        for i in range(6)
    ]
    report = metrics.build_report(rows, ["200"], ["a", "b"], resamples=200, seed=1)
    a = report["summary"]["200"]["a"]
    assert a["n"] == 6 and a["metrics"]["mrr"]["mean"] == 1.0
    assert set(a["by_form"]) == {"keyword", "person_time"}
    diff = report["paired_a_minus_b"]["200"]["a-b"]["mrr"]
    assert diff["difference"] == pytest.approx(0.5) and diff["n_clusters"] == 3
    assert report["warnings"]  # 묶음이 10개 미만


# ---------------------------------------------------------------- 질의 선택 (커밋된 v1 데이터)


def test_query_selection_counts_match_adr() -> None:
    world = worldmail.load_world(DATASET / "world.json")
    sizes = ("200", "1000", "5000")
    dev = qmod.select_queries(DATASET, "dev", world, sizes)
    test = qmod.select_queries(DATASET, "test", world, sizes)
    assert (dev.counts["selected"], dev.counts["clusters"]) == (37, 13)
    assert (test.counts["selected"], test.counts["clusters"]) == (51, 19)
    assert (
        dev.counts["document_answer_excluded"] == len(dev.counts["document_answer_query_ids"]) > 0
    )
    for sel in (dev, test):
        c = sel.counts
        assert (
            c["selected"] + c["document_answer_excluded"] + c["review_excluded"]
            == (c["in_split_before_review"])
        )
        assert all(2 in q.relevance[s].values() for q in sel.queries for s in sizes)
        assert all(
            not k.startswith("doc-") for q in sel.queries for r in q.relevance.values() for k in r
        )
    assert not {q.query_id for q in dev.queries} & set(dev.counts["document_answer_query_ids"])


# ---------------------------------------------------------------- 방식·CLI


def test_parse_methods() -> None:
    assert parse_methods("all") == METHODS
    assert parse_methods("bm25, keyword,bm25") == ("bm25", "keyword")
    with pytest.raises(ValueError):
        parse_methods("nope")


def test_test_split_requires_flag_and_ledger_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["run", "--dataset", str(DATASET), "--split", "test", "--dry-run"]
    assert main(args) == 2
    assert "봉인" in capsys.readouterr().err
    assert _record_test_run(tmp_path, "e1") == 1
    assert _record_test_run(tmp_path, "e2") == 2
    assert main([*args, SEAL_FLAG, "--embed-cache-dir", str(tmp_path / "c")]) == 0


def test_dry_run_reports_without_calls_or_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / "cache"
    code = main(
        [
            "run",
            "--dataset",
            str(DATASET),
            "--split",
            "dev",
            "--dry-run",
            "--embed-cache-dir",
            str(cache),
        ]
    )
    assert code == 0 and not cache.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["calls_made"] == 0 and set(report["accounts"]) == {"200", "1000", "5000"}
    assert report["accounts"]["200"]["mails"] == 200
    assert report["total"]["est_tokens"] > 0 and report["total"]["est_cost_usd"] > 0
    assert report["queries_selected"]["selected"] == 37


def test_dry_run_counts_cached_chunks(tmp_path: Path) -> None:
    world = worldmail.load_world(DATASET / "world.json")
    chunks = worldmail.mailbox_chunks(world, "acct-s200")
    path = cache_path(tmp_path, "m")
    path.write_text(
        "".join(json.dumps({"k": text_key(c), "v": ""}) + "\n" for c in chunks), encoding="utf-8"
    )
    report = dry_run_report(
        world=world, sizes=["200"], accounts={"200": "acct-s200"}, query_texts=[], model_id="m",
        cache_dir=tmp_path, price_per_mtok=1.0, needs_query_embedding=False,
    )  # fmt: skip
    acc = report["accounts"]["200"]
    assert acc["new_chunks"] == 0 and acc["est_tokens"] == 0 and acc["chunks"] == len(chunks)


def test_existing_output_is_not_overwritten(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    out.write_text("keep", encoding="utf-8")
    args = ["run", "--dataset", str(DATASET), "--split", "dev", "--out", str(out)]
    assert main(args) == 2 and out.read_text(encoding="utf-8") == "keep"


def test_runs_directory_is_git_ignored() -> None:
    try:
        done = subprocess.run(  # noqa: S603
            ["git", "-C", str(REPO), "check-ignore", "runs/embed-cache/x.jsonl"],  # noqa: S607
            capture_output=True,
            check=False,
        )
    except OSError:
        pytest.skip("git을 쓸 수 없다")
    assert done.returncode == 0
