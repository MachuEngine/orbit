"""rag-v1 통계: 유형·분할별 문항 수, 근거 메일 길이 분포, 긴 근거 비율."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from orbit.evaldata.rag_v1.model import Plan
from orbit.evaldata.schema import Task
from orbit.evaldata.v1.facts import norm
from orbit.evaldata.v1.skeleton import Skeleton

LONG_CHARS = 800  # 긴 근거의 기준(글자 수)
MIN_LONG_RATIO = 0.30  # 근거 메일 중 긴 메일의 최소 비율
LATE_NOTE = "미검증: 사실이 본문 후반부에 있는지는 확인하지 않았다(첫 등장 위치만 측정)"


def percentile(values: Sequence[int], q: float) -> int:
    """최근접 순위 백분위수. 비어 있으면 0."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def length_summary(lengths: Sequence[int]) -> dict[str, int]:
    return {
        "n": len(lengths),
        "p50": percentile(lengths, 0.5),
        "p90": percentile(lengths, 0.9),
        "p95": percentile(lengths, 0.95),
        "max": max(lengths, default=0),
    }


def compute_stats(
    plan: Plan,
    tasks: Sequence[Task],
    skeleton: Skeleton,
    bodies: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """`bodies`(메일 ID -> 실현된 본문)가 있으면 실제 길이와 사실 위치를 함께 계산한다."""
    by_type: Counter[str] = Counter()
    by_split: Counter[str] = Counter()
    by_type_split: Counter[str] = Counter()
    for t in tasks:
        kind = next(x.removeprefix("rag:") for x in t.tags if x.startswith("rag:"))
        by_type[kind] += 1
        by_split[t.split] += 1
        by_type_split[f"{kind}/{t.split}"] += 1
    body_of = {m.id: m.body_id for a in skeleton.accounts.values() for m in a.messages}
    evidence = {
        "all": sorted({m for bp in plan.blueprints for m in bp.evidence}),
        "dev": sorted(
            {
                m
                for bp in plan.blueprints
                if plan.cluster(bp.cluster_id).split == "dev"
                for m in bp.evidence
            }
        ),
    }
    planned_long = [skeleton.bodies[body_of[m]].length_class == "long" for m in evidence["all"]]
    out: dict[str, Any] = {
        "schema_version": plan.schema_version,
        "seed": plan.seed,
        "generator_git_sha": plan.generator_git_sha,
        "tasks": len(tasks),
        "by_type": dict(sorted(by_type.items())),
        "by_split": dict(sorted(by_split.items())),
        "by_type_split": dict(sorted(by_type_split.items())),
        "clusters": len(plan.clusters),
        "mails_per_account": {a: len(acc.messages) for a, acc in skeleton.accounts.items()},
        "evidence_mails": len(evidence["all"]),
        "fact_late_position": LATE_NOTE,
        "planned_long_evidence_ratio": round(sum(planned_long) / max(1, len(planned_long)), 4),
    }
    if bodies is None:
        out["realized"] = False
        return out
    ev_lengths = {k: [len(bodies[m]) for m in ids if m in bodies] for k, ids in evidence.items()}
    long_n = sum(1 for n in ev_lengths["all"] if n > LONG_CHARS)
    positions: list[float] = []
    for bp in plan.blueprints:
        for g in bp.facts:
            for m in bp.evidence:
                text = norm(bodies.get(m, ""))
                hits = [text.find(norm(f)) for f in g.any_of if norm(f) in text]
                if hits:
                    positions.append(min(hits) / max(1, len(text)))
                    break
    out.update(
        realized=True,
        dev_evidence_length=length_summary(ev_lengths["dev"]),
        all_evidence_length=length_summary(ev_lengths["all"]),
        long_evidence_ratio=round(long_n / max(1, len(ev_lengths["all"])), 4),
        long_threshold_chars=LONG_CHARS,
        fact_position_p50=round(sorted(positions)[len(positions) // 2], 3) if positions else None,
    )
    return out
