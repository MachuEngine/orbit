#!/usr/bin/env python3
"""G3 주입 판정기 선별 (ADR-0033). 판정기(llm·jev·clf)를 본문 단위로 돌려 탐지율과 오탐률을 낸다.

사용:
  backend/.venv/bin/python scripts/safety_detector_screen.py --max-cost-usd 1.0 \\
      [--detector llm --detector jev --detector clf] [--normal-samples 200] [--out screen.json]

입력(모두 dev 분할만 읽는다. test 분할 과제와 그 계정은 읽지 않는다):
- 공격 본문: safety-v1 dev 공격 과제의 `attack_source_ids` 본문(공격 계정)
- 짝 정상 본문: 같은 ID의 본문을 정상 쌍 계정에서 읽은 것
- 정상 메일: orbit-eval v1 dev 과제의 계정에서 고정 시드로 뽑은 N통(기본 200)

출력(JSON): 판정기별 탐지율(공격 본문), 오탐률(짝 정상 본문·정상 메일 각각), 판정 지연 p50/p95(ms),
비용(USD). 본문 원문은 출력하지 않는다. llm·jev는 비용이 든다. 누적 비용이 `--max-cost-usd`를
넘으면 그 판정기는 거기서 멈추고 `stopped_by_budget`을 true로 낸다(남은 본문은 세지 않는다).
Jev는 `AI_GATEWAY_API_KEY`, llm은 `ORBIT_LLM_*`가 필요하다. clf는 로컬에서 돈다.
"""

import argparse
import asyncio
import json
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orbit.core.config import get_llm_settings
from orbit.evalrunner.guards import (
    CLF_CANDIDATES,
    DEFAULT_CLF_MODEL,
    INJECTION_CLF,
    INJECTION_JEV,
    INJECTION_LLM,
    ClassifierContentJudge,
    LLMContentJudge,
    LocalHfClassifier,
)
from orbit.evalrunner.retrieval.worldmail import load_world
from orbit.evalrunner.task_source import load_dataset
from orbit.llm.factory import build_llm
from orbit.runflow.guards import ContentGuard
from orbit.runflow.injection import JevClient, JevContentJudge

ROOT = Path(__file__).resolve().parents[1]
SAFETY_DIR = ROOT / "eval/datasets/orbit-eval/safety-v1"
V1_DIR = ROOT / "eval/datasets/orbit-eval/v1"
DETECTORS = {"llm": INJECTION_LLM, "jev": INJECTION_JEV, "clf": INJECTION_CLF}


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)


def collect_bodies(normal_samples: int, seed: int) -> dict[str, list[str]]:
    """dev 분할에서 세 묶음(attack·pair_normal·normal_mail)의 본문을 모은다."""
    safety = load_dataset(SAFETY_DIR)
    world = load_world(SAFETY_DIR / "world.json")
    dev = [t for t in safety.tasks if t.split == "dev" and t.safety is not None]
    normal_by_pair = {t.safety.pair_id: t for t in dev if t.safety and t.safety.role == "normal"}
    out: dict[str, list[str]] = {"attack": [], "pair_normal": [], "normal_mail": []}
    for task in dev:
        info = task.safety
        if info is None or info.role != "attack":
            continue
        pair = normal_by_pair.get(info.pair_id)
        for source_id in info.attack_source_ids:
            out["attack"].append(_body(world.account(task.account_id), source_id))
            if pair is not None:
                out["pair_normal"].append(_body(world.account(pair.account_id), source_id))
    v1 = load_dataset(V1_DIR)
    v1_world = load_world(V1_DIR / "world.json")
    accounts = sorted({t.account_id for t in v1.tasks if t.split == "dev"})
    pool = sorted(
        (a, m.id) for a in accounts for m in v1_world.account(a).messages.values() if m.body_text
    )
    rng = random.Random(seed)  # noqa: S311  재현 가능한 표본 추출용
    for account, message_id in rng.sample(pool, min(normal_samples, len(pool))):
        out["normal_mail"].append(v1_world.account(account).messages[message_id].body_text)
    return out


def _body(account: Any, source_id: str) -> str:
    if source_id in account.messages:
        return str(account.messages[source_id].body_text)
    return str(account.files[source_id].text)


async def run_detector(
    judge: ContentGuard, bodies: dict[str, list[str]], max_cost_usd: float
) -> dict[str, Any]:
    spent = 0.0
    cost_unknown = False
    latencies: list[float] = []
    groups: dict[str, dict[str, int]] = {}
    stopped = False
    for group, texts in bodies.items():
        counts = {"n": 0, "flagged": 0, "errors": 0}
        for text in texts:
            if spent >= max_cost_usd:
                stopped = True
                break
            verdict = await judge.judge(text)
            counts["n"] += 1
            if verdict.error:
                counts["errors"] += 1
            elif verdict.flagged:
                counts["flagged"] += 1
            if not verdict.cached:
                latencies.append(verdict.latency_ms)
            if verdict.cost_usd is None:
                cost_unknown = True
            else:
                spent += verdict.cost_usd
        groups[group] = counts

    def rate(group: str) -> float | None:
        c = groups.get(group)
        return round(c["flagged"] / c["n"], 4) if c and c["n"] else None

    return {
        "detection_rate_attack": rate("attack"),
        "false_positive_rate_pair_normal": rate("pair_normal"),
        "false_positive_rate_normal_mail": rate("normal_mail"),
        "counts": groups,
        "latency_ms_p50": _pct(latencies, 0.5),
        "latency_ms_p95": _pct(latencies, 0.95),
        "cost_usd": round(spent, 6),
        "cost_unknown": cost_unknown,
        "stopped_by_budget": stopped,
    }


def build_judge(name: str, clf_model: str) -> ContentGuard:
    if name == "llm":
        return LLMContentJudge(build_llm(get_llm_settings()))
    if name == "jev":
        return JevContentJudge(JevClient())
    classifier = LocalHfClassifier(clf_model)
    classifier.load()
    return ClassifierContentJudge(classifier)


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    bodies = collect_bodies(args.normal_samples, args.seed)
    result: dict[str, Any] = {
        "counts_input": {k: len(v) for k, v in bodies.items()},
        "seed": args.seed,
        "clf_model": args.clf_model if "clf" in args.detector else None,
        "max_cost_usd": args.max_cost_usd,
        "detectors": {},
    }
    for name in args.detector:
        # 판정기마다 예산을 따로 쓴다. 합계 상한은 판정기 수 x 값이므로 합계도 낸다.
        result["detectors"][name] = await run_detector(
            build_judge(name, args.clf_model), bodies, args.max_cost_usd
        )
    result["total_cost_usd"] = round(sum(d["cost_usd"] for d in result["detectors"].values()), 6)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--max-cost-usd", type=float, required=True, help="판정기별 비용 상한")
    p.add_argument("--detector", action="append", choices=sorted(DETECTORS), default=None)
    p.add_argument("--clf-model", choices=sorted(CLF_CANDIDATES), default=DEFAULT_CLF_MODEL)
    p.add_argument("--normal-samples", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.max_cost_usd <= 0:
        print("--max-cost-usd는 0보다 커야 한다", file=sys.stderr)
        return 2
    args.detector = args.detector or ["clf", "jev", "llm"]
    try:
        result = asyncio.run(main_async(args))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"판정기 준비 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
