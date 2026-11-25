"""safety-v1 쌍 검수 기록. 쌍마다 한 줄이며 사람이 채운다.

`review.jsonl`(과제별 `ReviewEntry`, 추가 필드 금지)과 섞이지 않게 `pair-review.jsonl`을 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PAIR_REVIEW = "pair-review.jsonl"
CHECKS = ("normal_task_answerable", "attack_rule_correct", "no_real_brand")
DECISIONS = ("approve", "fix", "exclude")


def read_pair_review(root: Path) -> list[dict[str, Any]]:
    lines = (root / PAIR_REVIEW).read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines if x.strip()]


def unreviewed(rows: list[dict[str, Any]]) -> list[str]:
    """검수가 끝나지 않은 pair_id. 세 점검이 모두 true이고 decision이 approve여야 끝난 것이다."""
    out = []
    for r in rows:
        done = all(r.get(c) is True for c in CHECKS) and r.get("decision") == "approve"
        excluded = r.get("decision") == "exclude" and bool(str(r.get("note", "")).strip())
        if not (done or excluded):
            out.append(str(r.get("pair_id")))
    return out


def excluded_pairs(root: Path) -> set[str]:
    """검수에서 제외된 pair_id. 쌍 제외 규칙은 여기에만 있고 `load_dataset`이 쓴다."""
    if not (root / PAIR_REVIEW).exists():
        return set()
    return {str(r["pair_id"]) for r in read_pair_review(root) if r.get("decision") == "exclude"}
