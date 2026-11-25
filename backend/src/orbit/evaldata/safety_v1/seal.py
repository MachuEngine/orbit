"""safety-v1 test 봉인. rag-v1 봉인과 같은 규칙이고 쌍 검수가 끝나야 봉인된다."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orbit.evaldata import dataset as ds
from orbit.evaldata.rag_v1.seal import (
    GUIDE,
    SealError,
    _file_sha256,
    _rows_sha256,
    _tasks,
    _test_rows,
)
from orbit.evaldata.safety_v1.review import PAIR_REVIEW, read_pair_review, unreviewed
from orbit.evaldata.schema import Manifest

_INPUTS = {"world_sha256": ds.WORLD, "tasks_sha256": ds.TASKS, "pair_review_sha256": PAIR_REVIEW}


def _measure(root: Path) -> tuple[dict[str, Any], list[str]]:
    m: dict[str, Any] = {}
    problems = []
    try:
        rows = _test_rows(_tasks(root))
        m.update(
            test_items=len(rows),
            test_clusters=len({r["cluster_id"] for r in rows}),
            test_sha256=_rows_sha256(rows),
        )
    except (OSError, ValueError) as exc:
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
    missing = [f for f in (ds.WORLD, ds.TASKS, PAIR_REVIEW) if not (root / f).is_file()]
    if missing:
        raise SealError(f"봉인에 필요한 파일이 없다: {', '.join(missing)}")
    manifest = Manifest.model_validate_json((root / ds.MANIFEST).read_text(encoding="utf-8"))
    if manifest.seal is not None:
        raise SealError("이미 봉인되었다. 덮어쓰지 않는다")
    pending = unreviewed(read_pair_review(root))
    if pending:
        raise SealError(f"검수가 끝나지 않은 쌍이 {len(pending)}개 있다: {', '.join(pending[:5])}")
    m, problems = _measure(root)
    if problems:
        raise SealError("; ".join(problems))
    block = {"schema": "safety-seal-v1", "sealed_at": now().isoformat(), **m, "note": review_note}
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
