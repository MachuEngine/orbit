"""rag-v1 test 문항 봉인. v1 `auto_review.seal_dataset`의 최소판(검수 기록·검색 매니페스트 없음)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orbit.evaldata import dataset as ds
from orbit.evaldata.schema import Manifest

SAMPLE_FILE, GRAPH_FILE, SKELETON_FILE = "review-sample.md", "graph_gold.json", "skeleton.json"
SAMPLE_SEED = 20261121
SAMPLE_SCRIPT = "scripts/rag_v1_review_sample.py"
_INPUTS = {
    "world_sha256": ds.WORLD,
    "tasks_sha256": ds.TASKS,
    "graph_gold_sha256": GRAPH_FILE,
    "skeleton_sha256": SKELETON_FILE,
}
_ITEM_HEAD = re.compile(r"^## \S+ \((?:dev|test)\b", re.M)
GUIDE = (
    "봉인 뒤 test 문항을 직접 고치지 않는다. 정정이 필요하면 ADR-0010 절차대로 "
    "정정 기록을 따로 남기고 데이터셋 버전을 올린다"
)


class SealError(Exception):
    pass


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tasks(root: Path) -> list[dict[str, Any]]:
    lines = (root / ds.TASKS).read_text(encoding="utf-8").splitlines()
    tasks = [json.loads(x) for x in lines if x.strip()]
    for t in tasks:
        if not isinstance(t, dict) or not {"task_id", "split", "cluster_id"} <= t.keys():
            raise ValueError("task_id·split·cluster_id가 없는 줄이 있다")
    return tasks


def _test_rows(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((t for t in tasks if t["split"] == "test"), key=lambda t: str(t["task_id"]))


def _rows_sha256(rows: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def _measure(root: Path) -> tuple[dict[str, Any], list[str]]:
    """현재 해시·수량과 읽지 못한 항목 목록."""
    m: dict[str, Any] = {}
    problems = []
    try:
        rows = _test_rows(_tasks(root))
        m.update(
            test_items=len(rows),
            test_clusters=len({r["cluster_id"] for r in rows}),
            test_sha256=_rows_sha256(rows),
        )
    except (OSError, ValueError) as exc:  # JSONDecodeError는 ValueError
        problems.append(f"{ds.TASKS}를 읽을 수 없다 또는 형식이 잘못되었다({type(exc).__name__})")
    for key, name in _INPUTS.items():
        try:
            m[key] = _file_sha256(root / name)
        except OSError:
            problems.append(f"{name}이 없거나 읽을 수 없다")
    return m, problems


def seal_dataset(
    out: str | Path, review_note: str, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> dict[str, Any]:
    root = Path(out)
    if not review_note.strip():
        raise SealError("--review-note가 비어 있다")
    needed = (ds.WORLD, ds.TASKS, GRAPH_FILE, SKELETON_FILE, SAMPLE_FILE)
    missing = [f for f in needed if not (root / f).is_file()]
    if missing:
        raise SealError(f"봉인에 필요한 파일이 없다: {', '.join(missing)}")
    manifest = Manifest.model_validate_json((root / ds.MANIFEST).read_text(encoding="utf-8"))
    if manifest.seal is not None:
        raise SealError("이미 봉인되었다. 덮어쓰지 않는다")
    sampled = len(_ITEM_HEAD.findall((root / SAMPLE_FILE).read_text(encoding="utf-8")))
    m, problems = _measure(root)
    if problems:
        raise SealError("; ".join(problems))
    block = {
        "schema": "rag-seal-v1",
        "sealed_at": now().isoformat(),
        **m,
        "review": {
            "human_sampled": sampled,
            "sample_seed": SAMPLE_SEED,
            "sample_script": SAMPLE_SCRIPT,
            "note": review_note,
        },
    }
    ds.write_manifest(root, manifest.model_copy(update={"seal": block}))
    return block


def verify_seal(out: str | Path) -> list[str] | None:
    """봉인 전이면 None, 아니면 달라진 항목 목록(같으면 빈 목록)."""
    root = Path(out)
    seal = Manifest.model_validate_json((root / ds.MANIFEST).read_text(encoding="utf-8")).seal
    if seal is None:
        return None
    now, problems = _measure(root)
    for key, value in now.items():
        if seal.get(key) != value:
            problems.append(f"{key}이(가) 봉인 때와 다르다")
    return problems


def cmd_seal(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    try:
        block = seal_dataset(args.out, args.review_note)
    except SealError as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return 2
    echo(json.dumps(block, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_verify(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    problems = verify_seal(args.out)
    if problems is None:
        echo("봉인 전")
        return 0
    if not problems:
        echo("봉인 일치")
        return 0
    for p in problems:
        echo(p)
    echo(GUIDE)
    return 1
