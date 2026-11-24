"""reranker 후보 순위 비교(오프라인 진단). LLM·API 호출이 없다.

저장된 A1 dev 실행의 1단계 후보 20통(`first_stage_ranked_message_ids`)을 후보 reranker마다
원래 질문으로 다시 정렬하고, gold 근거 메일 기준 recall@5·MRR·처리 시간을 잰다.
A1 실행 때의 질의는 LLM 재작성문이라 저장되지 않았다(ADR-0006). 그래서 모든 후보에 원래 질문을 쓴다.

사용법: cd backend && uv run --group rag-eval python ../scripts/rag_v1_reranker_compare.py \
    ../eval/datasets/orbit-eval/rag-v1 ../eval/runs/rag-arch-v1/dev-A1.jsonl \
    ../eval/runs/rag-arch-v1/reranker-compare-dev.json
"""

import json
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from orbit.evalrunner.rag_arch.a1 import rerank_passage
from orbit.evalrunner.rag_arch.config import RERANKER_MAX_LENGTH, RERANKER_REVISION
from orbit.evalrunner.rag_arch.sut import oracle_message_ids
from orbit.evalrunner.task_source import load_dataset

TOP_K, BOOT, SEED = 5, 10_000, 0
# (이름, model_id, revision, 방식). revision은 2026-11-24 main 커밋.
CANDIDATES = [
    ("bge-m3", "BAAI/bge-reranker-v2-m3", RERANKER_REVISION, "cross"),
    (
        "bge-m3-ko",
        "dragonkue/bge-reranker-v2-m3-ko",
        "2aca5884ecac490192af9ebd86836d9073d826cd",
        "cross",
    ),
    ("ko-reranker", "Dongjin-kr/ko-reranker", "8c2ab1a833734491fd3158164dd2b7d8b28e071c", "cross"),
    ("qwen3-0.6b", "Qwen/Qwen3-Reranker-0.6B", "e61197ed45024b0ed8a2d74b80b4d909f1255473", "qwen3"),
]


class Cross:
    def __init__(self, model_id: str, revision: str) -> None:
        from sentence_transformers import CrossEncoder
        from transformers import AutoConfig

        # ko-reranker는 위치 임베딩이 514라 768을 못 받는다. 모델 한도 안으로 줄인다.
        limit = AutoConfig.from_pretrained(model_id, revision=revision).max_position_embeddings - 2
        self.m = CrossEncoder(
            model_id, revision=revision, max_length=min(RERANKER_MAX_LENGTH, limit), device="cpu"
        )
        self.m.model.eval()

    def score(self, q: str, ps: list[str]) -> list[float]:
        return [float(x) for x in self.m.predict([(q, p) for p in ps], show_progress_bar=False)]


class Qwen3:
    """모델 카드 방식: 지시문 + 질의/문서 → 마지막 토큰의 yes/no logit으로 점수."""

    PREFIX = (
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query "
        'and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
        "<|im_start|>user\n"
    )
    SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    INSTRUCT = "Given a web search query, retrieve relevant passages that answer the query"

    def __init__(self, model_id: str, revision: str) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(model_id, revision=revision, padding_side="left")
        self.m = AutoModelForCausalLM.from_pretrained(model_id, revision=revision).eval()
        self.yes = self.tok.convert_tokens_to_ids("yes")
        self.no = self.tok.convert_tokens_to_ids("no")
        self.pre = self.tok.encode(self.PREFIX, add_special_tokens=False)
        self.suf = self.tok.encode(self.SUFFIX, add_special_tokens=False)
        self.max_len = RERANKER_MAX_LENGTH + 256  # 지시문·템플릿 토큰만큼 더 준다

    def score(self, q: str, ps: list[str]) -> list[float]:
        texts = [f"<Instruct>: {self.INSTRUCT}\n<Query>: {q}\n<Document>: {p}" for p in ps]
        enc = self.tok(
            texts,
            truncation="longest_first",
            add_special_tokens=False,
            max_length=self.max_len - len(self.pre) - len(self.suf),
        )
        enc["input_ids"] = [self.pre + ids + self.suf for ids in enc["input_ids"]]
        batch = self.tok.pad(enc, padding=True, return_tensors="pt")
        with self.torch.no_grad():
            logits = self.m(**batch).logits[:, -1, :]
        pair = self.torch.stack([logits[:, self.no], logits[:, self.yes]], dim=1)
        return self.torch.log_softmax(pair, dim=1)[:, 1].exp().tolist()


def load_rows(dataset_dir: Path, results: Path) -> list[dict[str, Any]]:
    ds = load_dataset(dataset_dir)
    gold = oracle_message_ids(ds, "dev")
    questions = {
        t.task_id: [x.content for x in t.turns if x.role == "user"]
        for t in ds.tasks
        if t.split == "dev"
    }
    world = ds.world_snapshot
    mails = {
        m["id"]: rerank_passage({"subject": m.get("subject", ""), "body": m.get("body_text", "")})
        for acc in world["accounts"].values()
        for m in acc["messages"]
    }
    rows = []
    for line in results.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        g = gold.get(r["task_id"], ())
        q = questions.get(r["task_id"], [])
        if not g or len(q) != 1:
            continue
        cand = r["rag_diag"]["first_stage_ranked_message_ids"]
        rows.append(
            {
                "task_id": r["task_id"],
                "q": q[0],
                "cand": cand,
                "gold": set(g),
                "passages": [mails[c] for c in cand],
            }
        )
    return rows


def metrics(order: list[str], gold: set[str]) -> tuple[float, float]:
    recall = len(gold & set(order[:TOP_K])) / len(gold)
    mrr = next((1 / (i + 1) for i, c in enumerate(order) if c in gold), 0.0)
    return recall, mrr


def boot_diff(a: dict[str, list[float]], b: dict[str, list[float]]) -> tuple[float, float, float]:
    """과제 묶음 부트스트랩 95% CI. a, b는 과제 ID -> 행별 값(같은 순서)."""
    tasks = sorted(a)
    diffs = {t: [x - y for x, y in zip(a[t], b[t], strict=True)] for t in tasks}
    mean = lambda ts: sum(sum(diffs[t]) for t in ts) / sum(len(diffs[t]) for t in ts)  # noqa: E731
    rng = random.Random(SEED)  # noqa: S311 - 재현용 부트스트랩
    samples = sorted(mean(rng.choices(tasks, k=len(tasks))) for _ in range(BOOT))
    return mean(tasks), samples[int(0.025 * BOOT)], samples[int(0.975 * BOOT) - 1]


def main() -> int:
    dataset_dir, results, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    rows = load_rows(dataset_dir, results)
    ceiling = statistics.mean(len(r["gold"] & set(r["cand"])) / len(r["gold"]) for r in rows)
    hybrid = [metrics(r["cand"], r["gold"]) for r in rows]
    tasks = len({r["task_id"] for r in rows})
    print(f"rows={len(rows)} tasks={tasks} recall@20(상한)={ceiling:.3f}")
    report: dict[str, Any] = {"rows": len(rows), "recall@20": ceiling, "models": {}}
    per_task: dict[str, dict[str, list[float]]] = {}
    for name, model_id, rev, kind in CANDIDATES:
        model = Cross(model_id, rev) if kind == "cross" else Qwen3(model_id, rev)
        recalls, mrrs, ms = [], [], []
        by_task: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            t0 = time.perf_counter()
            scores = model.score(r["q"], r["passages"])
            ms.append((time.perf_counter() - t0) * 1000)
            order = [c for _, c in sorted(zip(scores, r["cand"], strict=True), key=lambda x: -x[0])]
            rec, mrr = metrics(order, r["gold"])
            recalls.append(rec)
            mrrs.append(mrr)
            by_task[r["task_id"]].append(rec)
        per_task[name] = by_task
        ms.sort()
        report["models"][name] = {
            "model_id": model_id,
            "revision": rev,
            "recall@5": statistics.mean(recalls),
            "mrr": statistics.mean(mrrs),
            "ms_p50": ms[len(ms) // 2],
            "ms_p95": ms[int(len(ms) * 0.95)],
            "recall@5_multi_gold": statistics.mean(
                x for x, r in zip(recalls, rows, strict=True) if len(r["gold"]) > 1
            ),
            "max_length": getattr(getattr(model, "m", None), "max_length", None),
        }
        row = report["models"][name]
        shown = {k: v for k, v in row.items() if k not in ("model_id", "revision")}
        print(name, shown)
        del model
    hy_task: dict[str, list[float]] = defaultdict(list)
    for r, (rec, _) in zip(rows, hybrid, strict=True):
        hy_task[r["task_id"]].append(rec)
    report["hybrid_order"] = {
        "recall@5": statistics.mean(x for x, _ in hybrid),
        "mrr": statistics.mean(y for _, y in hybrid),
    }
    report["diff_vs_bge-m3"] = {
        name: boot_diff(per_task[name], per_task["bge-m3"]) for name in per_task if name != "bge-m3"
    }
    report["diff_vs_bge-m3"]["hybrid_order"] = boot_diff(hy_task, per_task["bge-m3"])
    summary = {k: report[k] for k in ("hybrid_order", "diff_vs_bge-m3")}
    print(json.dumps(summary, ensure_ascii=False))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
