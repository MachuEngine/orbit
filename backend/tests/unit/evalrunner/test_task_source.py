"""러너의 데이터셋 경계: evaldata 위임과 러너 고유 검증을 본다."""

import json
from pathlib import Path

import pytest

from orbit.evalrunner.task_source import DatasetError, load_dataset

from .conftest import DATASET_DIR


def _copy(tmp_path: Path) -> Path:
    for name in ("manifest.json", "world.json", "tasks.jsonl"):
        (tmp_path / name).write_text((DATASET_DIR / name).read_text(encoding="utf-8"), "utf-8")
    return tmp_path


def _rows(root: Path) -> list[dict[str, object]]:
    lines = (root / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines]


def _write(root: Path, rows: list[dict[str, object]]) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    (root / "tasks.jsonl").write_text(text, encoding="utf-8")


def test_load_fixture_dataset_through_evaldata() -> None:
    ds = load_dataset(DATASET_DIR)
    assert (ds.name, ds.version) == ("mini-eval", "0.1.0")
    assert [t.task_id for t in ds.tasks_for("dev")] == ["t-001", "t-002", "t-003", "t-005"]
    assert [t.task_id for t in ds.tasks_for("test")] == ["t-004"]
    t2 = ds.tasks[1]
    assert t2.cluster_id == "c-t-001"
    assert t2.gold.expected_answer is not None and t2.gold.expected_answer.kind == "event_set"
    assert ds.tasks[0].gold.expected_answer is None
    assert ds.tasks[0].as_of_time.tzinfo is not None


def test_review_exclude_is_applied(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    entries = [
        {"task_id": "t-003", "decision": "exclude", "note": "검수 제외"},
        {"task_id": "t-001", "decision": "approve"},
    ]
    (root / "review.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), "utf-8")
    ds = load_dataset(root)
    assert "t-003" not in {t.task_id for t in ds.tasks}
    assert len(ds.tasks) == 4


def test_dataset_validation_failures_surface_as_dataset_error(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    rows = _rows(root)
    rows[0]["account_id"] = "acct-nobody"  # validate_dataset: world에 없는 계정
    _write(root, rows)
    with pytest.raises(DatasetError, match="acct-nobody"):
        load_dataset(root)
    rows = _rows(_copy(tmp_path))
    del rows[0]["cluster_id"]
    _write(root, rows)
    with pytest.raises(DatasetError, match="cluster_id"):
        load_dataset(root)


def test_schema_errors_are_dataset_errors_without_input_values(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    rows = _rows(root)
    rows[1]["gold"]["expected_answer"] = {"kind": "time_slot", "start": "SECRET-NAIVE"}  # type: ignore[index]
    _write(root, rows)
    with pytest.raises(DatasetError) as info:
        load_dataset(root)
    assert "SECRET-NAIVE" not in str(info.value)


def test_args_subset_must_be_comparable_with_call_summary(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    for bad in ({"body_text": "x"}, {"draft": {"description": "x"}}, {"query": "가" * 81}):
        rows = _rows(_copy(tmp_path))
        rows[0]["gold"]["required_tool_calls"] = [  # type: ignore[index]
            {"tool": "create_draft", "args_subset": bad}
        ]
        _write(root, rows)
        with pytest.raises(DatasetError, match="args_subset"):
            load_dataset(root)


def test_summary_constants_match_call_log() -> None:
    from orbit.connectors.sim import calls
    from orbit.evalrunner import task_source

    assert task_source.REDACTED_KEYS == calls._REDACTED_KEYS
    assert task_source.MAX_SUMMARY_STR == calls._MAX_STR
