"""RAG 아키텍처 비교(ADR-0036)의 진단 집계. 결과 JSONL과 데이터셋만 입력으로 받는다.

구성별(`config.sut_config.rag_arch`) 과제 성공률·pass^k·기준 구성과의 짝지은 차이
(`stats.py` 재사용),
유형별 성공률, 맥락 도달 재현율, 인용 정밀도·재현율, 효율 지표를 낸다. 정답 메일 ID는 데이터셋의
`expected_answer`(message_set)에서 읽는다. 결과 파일에는 정답이 없다.
예외로 ORACLE은 맥락·trace에 gold 메일 ID가 남으므로 dev 진단 전용이다.
"""

import json
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from orbit.evalrunner import stats
from orbit.evalrunner.metrics_v1 import percentile
from orbit.evalrunner.rag_arch.a3 import EXPECTED_ROUTE
from orbit.evalrunner.rag_arch.config import DIAGNOSTIC_ARCHS
from orbit.evalrunner.task_source import load_dataset

RAG_TYPES = ("rag:single", "rag:multihop", "rag:aggregate", "rag:unanswerable")
BASELINE = "A2"

Record = Mapping[str, Any]


def load_gold(dataset_dir: str | Path) -> tuple[dict[str, frozenset[str]], dict[str, str]]:
    """(과제 ID -> 정답 메일 ID 집합, 과제 ID -> rag 유형). message_set이 아니면 빈 집합."""
    gold: dict[str, frozenset[str]] = {}
    kinds: dict[str, str] = {}
    for task in load_dataset(dataset_dir).tasks:
        answer = task.gold.expected_answer
        messages = (
            getattr(answer, "messages", None)
            if getattr(answer, "kind", "") == "message_set"
            else None
        )
        gold[task.task_id] = frozenset(m.message_id for m in messages or [])
        kind = next((t for t in task.tags if t in RAG_TYPES), None)
        if kind is not None:
            kinds[task.task_id] = kind
    return gold, kinds


def _arch(records: Sequence[Record]) -> str:
    arch = ((records[0].get("config") or {}).get("sut_config") or {}).get("rag_arch")
    if not isinstance(arch, str):
        raise stats.ResultsError(
            "결과에 config.sut_config.rag_arch가 없다(rag_arch SUT 결과가 아니다)"
        )
    return arch


def _subset(records: Sequence[Record], ids: set[str]) -> list[Record]:
    """과제 부분집합. 중단 판정이 `config.n_tasks`를 쓰므로 부분집합 크기로 고쳐 쓴다."""
    rows = [r for r in records if r["task_id"] in ids]
    n = len({r["task_id"] for r in rows})
    return [{**r, "config": {**(r.get("config") or {}), "n_tasks": n}} for r in rows]


def _guard(fn: Any, *args: Any, **kw: Any) -> Any:
    try:
        return fn(*args, **kw)
    except stats.ResultsError as exc:
        return {"error": str(exc)}


def _success(records: Sequence[Record], resamples: int, seed: int) -> dict[str, Any]:
    s = stats.success_rate(records, resamples=resamples, seed=seed)
    return {
        "rate": s.rate,
        "ci": [s.ci_low, s.ci_high],
        "n_tasks": s.n_tasks,
        "n_clusters": s.n_clusters,
    }


def _paired(a: Sequence[Record], b: Sequence[Record], resamples: int, seed: int) -> dict[str, Any]:
    p = stats.paired_difference(a, b, resamples=resamples, seed=seed)
    return {"difference": p.difference, "ci": [p.ci_low, p.ci_high], "n_tasks": p.n_tasks}


def _cited(r: Record) -> set[str]:
    ids = r.get("answer_ids") or {}
    msgs = ids.get("messages") if isinstance(ids, Mapping) else None
    if not isinstance(msgs, list):
        return set()
    return {m["message_id"] for m in msgs if isinstance(m, Mapping) and "message_id" in m}


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def context_recall_and_citation(
    records: Sequence[Record], gold: Mapping[str, frozenset[str]]
) -> dict[str, Any]:
    """에피소드 평균. 정답 메일이 없는 과제는 재현율에서, 인용이 없는 에피소드는 정밀도에서 뺀다.

    진단이 없는 에피소드(오류로 끝남)는 도달 0으로 분모에 넣는다. 인용 재현율과 같은 에피소드
    집합이다. 인용 재현율은 답이 없거나 형식이 틀린 에피소드를 인용 0개(재현율 0)로 센다.
    """
    reach: list[float] = []
    prec: list[float] = []
    rec: list[float] = []
    no_diag = 0
    for r in records:
        truth = gold.get(r["task_id"], frozenset())
        diag = r.get("rag_diag")
        if diag is None:
            no_diag += 1
        if truth:
            reached = set((diag or {}).get("context_message_ids") or [])
            reach.append(len(truth & reached) / len(truth))
        cited = _cited(r)
        if cited:
            prec.append(len(cited & truth) / len(cited))
        if truth:
            rec.append(len(cited & truth) / len(truth))
    return {
        "context_recall": _mean(reach),
        "n_context_recall_episodes": len(reach),
        "n_episodes_without_diag": no_diag,
        "citation_precision": _mean(prec),
        "n_precision_episodes": len(prec),
        "citation_recall": _mean(rec),
        "n_recall_episodes": len(rec),
    }


RECALL_KS = (1, 3, 5, 10)


def ranked_recall(records: Sequence[Record], gold: Mapping[str, frozenset[str]]) -> dict[str, Any]:
    """검색 순위 recall@K 에피소드 평균(진단). 순위 키가 없는 레코드와 정답 메일이 없는 과제는 뺀다.

    A0는 `ranked_message_ids`, A1은 거기에 더해 1차 검색 순위 `first_stage_ranked_message_ids`.
    해당 키를 가진 레코드가 하나도 없으면(A2 등) 키를 내지 않는다.
    A3 결과에서는 A1 경로 에피소드만 센다(none·A2 경로는 순위 키가 없다)."""
    out: dict[str, Any] = {}
    for key, prefix, n_key in (
        ("ranked_message_ids", "", "n_ranked_episodes"),
        ("first_stage_ranked_message_ids", "first_stage_", "n_first_stage_episodes"),
    ):
        rows = [
            (gold[r["task_id"]], d[key])
            for r in records
            if (d := r.get("rag_diag")) is not None and key in d and gold.get(r["task_id"])
        ]
        if not rows:
            continue
        for k in RECALL_KS:
            out[f"{prefix}recall@{k}"] = _mean(
                [len(t & set(list(dict.fromkeys(ids))[:k])) / len(t) for t, ids in rows]
            )  # 중복 ID는 순서를 유지하며 뺀 뒤 자른다
        out[n_key] = len(rows)
    return out


def search_call_recall(
    records: Sequence[Record], gold: Mapping[str, frozenset[str]], k: int = 5
) -> dict[str, Any]:
    """검색 호출별 recall@k(진단). 호출마다 반환 상위 k통 기준이고 episode 안에서 호출을 평균한 뒤
    episode끼리 평균한다. `search_log`가 있는 레코드 중 검색을 1회 이상 했고 정답 메일이 있는 과제만
    센다. 해당 레코드가 하나도 없으면(A2R·A2X 밖) 빈 dict라서 기존 출력은 바뀌지 않는다."""
    per_episode: list[float] = []
    n_calls = 0
    for r in records:
        log = (r.get("rag_diag") or {}).get("search_log")
        truth = gold.get(r["task_id"])
        if not log or not truth:
            continue
        per_call = [
            len(truth & set(list(dict.fromkeys(c["returned_message_ids"]))[:k])) / len(truth)
            for c in log
        ]
        per_episode.append(statistics.fmean(per_call))
        n_calls += len(per_call)
    if not per_episode:
        return {}
    return {
        f"search_call_recall@{k}": statistics.fmean(per_episode),
        "n_search_call_recall_episodes": len(per_episode),
        "n_search_calls_scored": n_calls,
    }


def efficiency(records: Sequence[Record]) -> dict[str, Any]:
    calls = [r["usage"]["llm_calls"] for r in records]
    tokens = [r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in records]
    costs = [r["usage"]["cost_usd"] for r in records if r["usage"]["cost_usd"] is not None]
    latency = [r["latency_ms"] for r in records if r.get("latency_ms") is not None]
    diags = [r["rag_diag"] for r in records if r.get("rag_diag") is not None]
    return {
        "n_episodes": len(records),
        "llm_calls_mean": _mean(calls),
        "tokens_mean": _mean(tokens),
        "cost_usd_mean": _mean(costs),
        "n_cost_unknown": len(records) - len(costs),
        "latency_ms_p50": percentile(latency, 0.5),
        "latency_ms_p95": percentile(latency, 0.95),
        "search_calls_mean": _mean([d["search_calls"] for d in diags]),
        "truncated_rate": _mean([1.0 if d["truncated"] else 0.0 for d in diags]),
        "context_chars_mean": _mean([d["context_chars"] for d in diags]),
        "n_index_pending": sum(1 for d in diags if d.get("index_pending")),
        "answer_followup_rate": _mean(
            [1.0 if d["answer_followup"] else 0.0 for d in diags if "answer_followup" in d]
        ),
        "denied_calls_total": sum(len(d.get("denied_tools") or []) for d in diags),
    }


def a1_stats(records: Sequence[Record]) -> dict[str, Any] | None:
    """A1 진단 집계. A1 진단(`rewrite_fallback`)이 있는 에피소드가 없으면 None."""
    diags = [d for r in records if (d := r.get("rag_diag")) is not None and "rewrite_fallback" in d]
    if not diags:
        return None
    before = sum(d["compressed_chars_before"] for d in diags)
    after = sum(d["compressed_chars_after"] for d in diags)
    return {
        "n_episodes": len(diags),
        "rewrite_fallback_rate": _mean([1.0 if d["rewrite_fallback"] else 0.0 for d in diags]),
        "compress_fallback_total": sum(d["compress_fallback"] for d in diags),
        "compressed_empty_total": sum(d.get("compressed_empty", 0) for d in diags),
        "compressed_empty_mean": _mean([float(d.get("compressed_empty", 0)) for d in diags]),
        "compression_ratio": after / before if before else None,  # 압축 뒤 본문 / 원문 본문
    }


def a3_stats(
    records: Sequence[Record],
    kinds: Mapping[str, str],
    gold: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Any] | None:
    """A3 라우팅 집계. 라우터 진단(`a3_route`)이 있는 에피소드가 하나도 없으면 None.

    대상은 레코드에 있는 에피소드 전체다. 진단이 없거나 `a3_route`가 없는 에피소드는 오류로 끝난
    경우이므로 분모에 넣고 라우팅 오답으로 센다. 그 수는 `n_without_diag`로 따로 낸다.
    유형은 여기서만 과제 태그에서 읽는다(라우터에는 넘기지 않는다).
    - `routing_accuracy.accuracy`: 기대표(`EXPECTED_ROUTE`)와 맞은 에피소드 / 유형이 있는 전체
      에피소드. fallback, "검색 없음", 경로 없는 에피소드는 오답이다. 보조 필드
      `accuracy_excl_fallback`은 경로가 있고 fallback이 아닌 에피소드만 센다.
    - `none_rate`: "검색 없음" 에피소드 / 전체 에피소드.
    - `routes`: 경로가 있는 에피소드의 분포. fallback 에피소드는 A2 칸에 함께 들어간다
      (`n_fallback`으로 따로 센다).
    - `context_recall_by_route`: `gold`가 있으면 고른 경로별 맥락 도달 재현율. 경로 없는 에피소드는
      도달 0으로 `no_route` 칸에 따로 묶는다."""
    rows: list[tuple[str | None, Any, Any]] = []
    for r in records:
        d = r.get("rag_diag")
        d = d if d is not None and "a3_route" in d else None
        truth = gold.get(r["task_id"], frozenset()) if gold else frozenset()
        rows.append((kinds.get(r["task_id"]), d, truth))
    routed = [x for x in rows if x[1] is not None]
    if not routed:
        return None

    def accuracy(items: Sequence[tuple[str | None, Any, Any]]) -> dict[str, Any]:
        typed = [(k, d) for k, d, _ in items if k in EXPECTED_ROUTE]
        hit = [d is not None and not d["route_fallback"] and d["a3_route"] == EXPECTED_ROUTE[k]
               for k, d in typed]  # fmt: skip
        kept = [h for h, (_, d) in zip(hit, typed, strict=True) if d and not d["route_fallback"]]
        return {
            "accuracy": _mean([float(h) for h in hit]),
            "n": len(hit),
            "accuracy_excl_fallback": _mean([float(h) for h in kept]),
            "n_excl_fallback": len(kept),
            "n_fallback": sum(1 for _, d in typed if d and d["route_fallback"]),
            "n_without_diag": sum(1 for _, d in typed if d is None),
        }

    by_type: dict[str, Any] = {}
    for kind in RAG_TYPES:
        items = [x for x in rows if x[0] == kind]
        if items:
            by_type[kind] = {
                "routes": dict(sorted(Counter(d["a3_route"] for _, d, _ in items if d).items())),
                "routing_accuracy": accuracy(items),
            }
    by_route: dict[str, Any] = {}
    for route in sorted({d["a3_route"] for _, d, _ in routed}) + ["no_route"]:
        recalls = []
        for _, d, truth in rows:
            if truth and (d["a3_route"] if d else "no_route") == route:
                reached = set((d or {}).get("context_message_ids") or [])
                recalls.append(len(truth & reached) / len(truth))
        if recalls or route != "no_route":
            by_route[route] = {"context_recall": _mean(recalls), "n": len(recalls)}
    return {
        "n_episodes": len(rows),
        "n_without_diag": len(rows) - len(routed),
        "routes": dict(sorted(Counter(d["a3_route"] for _, d, _ in routed).items())),
        "none_rate": sum(1 for _, d, _ in routed if d["a3_route"] == "none") / len(rows),
        "n_fallback": sum(1 for _, d, _ in routed if d["route_fallback"]),
        "routing_accuracy": accuracy(rows),
        "by_type": by_type,
        "context_recall_by_route": by_route,
        "route_cost_usd_mean": _mean(
            [d["route_cost_usd"] for _, d, _ in routed if d.get("route_cost_usd") is not None]
        ),
    }


def diagnose(
    by_arch: Mapping[str, Sequence[Record]],
    gold: Mapping[str, frozenset[str]],
    kinds: Mapping[str, str],
    *,
    baseline: str = BASELINE,
    k: int | None = None,
    resamples: int = stats.DEFAULT_RESAMPLES,
    seed: int = stats.DEFAULT_SEED,
) -> dict[str, Any]:
    base = by_arch.get(baseline)
    out: dict[str, Any] = {"baseline": baseline, "archs": {}}
    for arch, records in sorted(by_arch.items()):
        entry: dict[str, Any] = {
            "success": _guard(_success, records, resamples, seed),
            "pass_k": _guard(lambda r=records: asdict(stats.pass_k(r, k))),
            "efficiency": efficiency(records),
            **context_recall_and_citation(records, gold),
            **ranked_recall(records, gold),
            **search_call_recall(records, gold),
        }
        if (a1 := a1_stats(records)) is not None:
            entry["a1"] = a1
        if (a3 := a3_stats(records, kinds, gold)) is not None:
            entry["a3"] = a3
        paired = base is not None and arch != baseline and arch not in DIAGNOSTIC_ARCHS
        if base is not None and paired:  # 진단 구성(ORACLE)은 주 비교(A2 대비 paired)에 넣지 않는다
            entry["paired_vs_baseline"] = _guard(_paired, records, base, resamples, seed)
        by_type: dict[str, Any] = {}
        for kind in RAG_TYPES:
            ids = {t for t, v in kinds.items() if v == kind} & {r["task_id"] for r in records}
            if not ids:
                continue
            item: dict[str, Any] = {
                "success": _guard(_success, _subset(records, ids), resamples, seed)
            }
            if base is not None and paired:
                item["paired_vs_baseline"] = _guard(
                    _paired, _subset(records, ids), _subset(base, ids), resamples, seed
                )
            by_type[kind] = item
        entry["by_type"] = by_type
        out["archs"][arch] = entry
    return out


def _conditions(records: Sequence[Record]) -> dict[str, Any]:
    config = records[0].get("config") or {}
    sut_config = config.get("sut_config") or {}
    return {
        "context_char_cap": sut_config.get("context_char_cap"),
        "index": sut_config.get("index"),
        "model_id": config.get("model_id"),
        "dataset": config.get("dataset"),
        "split": config.get("split"),
        "tasks": sorted({str(r["task_id"]) for r in records}),
    }


def check_comparable(by_arch: Mapping[str, Sequence[Record]]) -> None:
    """구성끼리 비교하려면 맥락 상한·모델·데이터셋·분할·과제 집합이 같아야 한다."""
    items = [(arch, _conditions(records)) for arch, records in by_arch.items() if records]
    for arch, cond in items[1:]:
        for key, value in cond.items():
            if value != items[0][1][key]:
                raise stats.ResultsError(f"{items[0][0]}와 {arch}의 {key}가 다르다")


def run_cli(args: Any) -> int:
    gold, kinds = load_gold(args.dataset)
    by_arch: dict[str, list[Record]] = {}
    for path in args.results:
        records = stats.load_results(path, reject_model_mismatch=True)
        arch = _arch(records)
        if arch in by_arch:
            raise stats.ResultsError(f"같은 구성({arch})의 결과가 둘 이상이다")
        by_arch[arch] = records
    check_comparable(by_arch)
    report = diagnose(
        by_arch,
        gold,
        kinds,
        baseline=args.baseline,
        k=args.k,
        resamples=args.resamples,
        seed=args.seed,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0
