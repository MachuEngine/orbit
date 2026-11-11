import json
from pathlib import Path

import pytest

from orbit.evalrunner.task_source import DatasetError, load_dataset

from .conftest import DATASET_DIR


def test_load_fixture_dataset() -> None:
    ds = load_dataset(DATASET_DIR)
    assert (ds.name, ds.version) == ("mini-eval", "0.1.0")
    assert [t.task_id for t in ds.tasks_for("dev")] == ["t-001", "t-002", "t-003", "t-005"]
    assert [t.task_id for t in ds.tasks_for("test")] == ["t-004"]
    t = ds.tasks[0]
    assert t.as_of_time.tzinfo is not None
    assert t.gold.expected_changes == ()
    assert ds.tasks[4].gold.expected_changes == ({"type": "event_created"},)


def _copy(tmp_path: Path) -> Path:
    for name in ("manifest.json", "world.json", "tasks.jsonl"):
        (tmp_path / name).write_text((DATASET_DIR / name).read_text(encoding="utf-8"), "utf-8")
    return tmp_path


def _first_task(root: Path) -> dict[str, object]:
    first = (root / "tasks.jsonl").read_text(encoding="utf-8").splitlines()[0]
    data: dict[str, object] = json.loads(first)
    return data


def _write_task(root: Path, task: dict[str, object]) -> None:
    (root / "tasks.jsonl").write_text(json.dumps(task, ensure_ascii=False) + "\n", "utf-8")


def test_rejects_naive_as_of_time(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    task = _first_task(root)
    task["as_of_time"] = "2026-11-11T09:00:00"
    _write_task(root, task)
    with pytest.raises(DatasetError, match="시간대"):
        load_dataset(root)


def test_rejects_unknown_split_and_account(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    task = _first_task(root)
    task["split"] = "val"
    _write_task(root, task)
    with pytest.raises(DatasetError, match="split"):
        load_dataset(root)
    task["split"] = "dev"
    task["account_id"] = "acct-nobody"
    _write_task(root, task)
    with pytest.raises(DatasetError, match="계정"):
        load_dataset(root)


def test_rejects_missing_field_and_empty_any_of(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    task = _first_task(root)
    del task["gold"]
    _write_task(root, task)
    with pytest.raises(DatasetError, match="gold"):
        load_dataset(root)
    task = _first_task(_copy(tmp_path))
    gold = task["gold"]
    assert isinstance(gold, dict)
    gold["answer_facts"] = [{"fact_id": "f", "any_of": [], "description": ""}]
    _write_task(root, task)
    with pytest.raises(DatasetError, match="any_of"):
        load_dataset(root)


def test_rejects_duplicate_task_id(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    line = json.dumps(_first_task(root), ensure_ascii=False)
    (root / "tasks.jsonl").write_text(line + "\n" + line + "\n", "utf-8")
    with pytest.raises(DatasetError, match="중복"):
        load_dataset(root)
