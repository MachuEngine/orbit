"""데이터셋 디렉터리 읽기·쓰기와 검증.

디렉터리: `manifest.json`, `world.json`(Sim 스냅샷 v1), `tasks.jsonl`, (검수 후) `review.jsonl`.
쓰기는 항상 키 정렬·들여쓰기 고정·개행 `\\n`이라 같은 내용이면 바이트가 같다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from orbit.evaldata.schema import Manifest, ReviewEntry, Task

MANIFEST = "manifest.json"
WORLD = "world.json"
TASKS = "tasks.jsonl"
REVIEW = "review.jsonl"


class DatasetError(ValueError):
    """데이터셋 파일이 없거나 형식·일관성 검증에 실패했다."""


@dataclass(frozen=True)
class Dataset:
    manifest: Manifest
    world_dict: dict[str, Any]
    tasks: list[Task]
    review: dict[str, ReviewEntry] = field(default_factory=dict)


def describe_error(exc: ValidationError | json.JSONDecodeError) -> str:
    """입력값을 싣지 않고 오류 위치와 유형만 적는다(정답이나 본문이 있을 수 있다)."""
    if isinstance(exc, json.JSONDecodeError):
        return f"JSON 구문 오류: {exc.msg} (줄 {exc.lineno}, 열 {exc.colno})"
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(최상위)'}: {e['type']}" for e in exc.errors()
    )


def dumps_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def dumps_jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def write_manifest(out: Path, manifest: Manifest) -> None:
    (out / MANIFEST).write_text(dumps_json(manifest.model_dump(mode="json")), encoding="utf-8")


def write_dataset(
    out: str | Path, manifest: Manifest, world: dict[str, Any], tasks: list[Task]
) -> None:
    path = Path(out)
    path.mkdir(parents=True, exist_ok=True)
    write_manifest(path, manifest)
    (path / WORLD).write_text(dumps_json(world), encoding="utf-8")
    rows = [t.model_dump(mode="json") for t in tasks]
    (path / TASKS).write_text(dumps_jsonl(rows), encoding="utf-8")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(f"{path.name}을 읽을 수 없다: {exc}") from exc


def _read_jsonl(path: Path, parse: Callable[[Any], Any]) -> list[Any]:
    items: list[Any] = []
    for lineno, line in enumerate(_read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(parse(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise DatasetError(
                f"{path.name} {lineno}번째 줄이 올바르지 않다: {describe_error(exc)}"
            ) from exc
    return items


def read_review(path: str | Path) -> dict[str, ReviewEntry]:
    """review.jsonl을 읽는다. 같은 task_id가 여러 번 나오면 마지막 줄이 이긴다."""
    file = Path(path) / REVIEW
    if not file.exists():
        return {}
    entries: list[ReviewEntry] = _read_jsonl(file, ReviewEntry.model_validate)
    return {e.task_id: e for e in entries}


def load_dataset(path: str | Path, *, include_excluded: bool = False) -> Dataset:
    """데이터셋을 읽고 검증한다. 검수에서 제외된 과제는 기본으로 건너뛴다."""
    root = Path(path)
    try:
        manifest = Manifest.model_validate_json(_read_text(root / MANIFEST))
    except ValidationError as exc:
        raise DatasetError(f"manifest.json이 올바르지 않다: {describe_error(exc)}") from exc
    try:
        world = json.loads(_read_text(root / WORLD))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"world.json이 올바른 JSON이 아니다: {describe_error(exc)}") from exc
    tasks: list[Task] = _read_jsonl(root / TASKS, Task.model_validate)
    review = read_review(root)
    problems = validate_dataset(manifest, world, tasks, review)
    if problems:
        raise DatasetError("데이터셋 검증 실패:\n- " + "\n- ".join(problems))
    if not include_excluded:
        tasks = [
            t
            for t in tasks
            if not (t.task_id in review and review[t.task_id].decision == "exclude")
        ]
    return Dataset(manifest, world, tasks, review)


def validate_dataset(
    manifest: Manifest,
    world: dict[str, Any],
    tasks: list[Task],
    review: dict[str, ReviewEntry] | None = None,
) -> list[str]:
    """문제 목록을 돌려준다. 비어 있으면 통과다."""
    problems: list[str] = []
    accounts = world.get("accounts", {})
    ids = [t.task_id for t in tasks]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"task_id 중복: {dup}")
    scenario_splits: dict[str, set[str]] = {}
    for t in tasks:
        if t.account_id not in accounts:
            problems.append(f"{t.task_id}: account_id {t.account_id}가 world에 없다")
        if t.as_of_time.utcoffset() is None:
            problems.append(f"{t.task_id}: as_of_time에 시간대가 없다")
        scenario_splits.setdefault(t.scenario_id, set()).add(t.split)
    for sid, splits in sorted(scenario_splits.items()):
        if len(splits) > 1:
            problems.append(f"시나리오 {sid}의 과제가 dev와 test에 모두 있다")
    messages = sum(len(a.get("messages", [])) for a in accounts.values())
    events = sum(len(a.get("events", [])) for a in accounts.values())
    expected = (len(accounts), messages, events, len(tasks))
    c = manifest.counts
    if expected != (c.users, c.messages, c.events, c.tasks):
        problems.append(
            f"manifest.counts {(c.users, c.messages, c.events, c.tasks)}가 실제 {expected}와 다르다"
        )
    for tid in sorted(set(review or {}) - set(ids)):
        problems.append(f"review.jsonl에 없는 과제 {tid}가 있다")
    return problems
