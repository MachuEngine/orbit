"""스키마 위반은 DatasetError이고, 메시지에 입력값(정답·본문일 수 있다)을 싣지 않는다."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from orbit.evaldata.dataset import DatasetError, load_dataset

from .conftest import COMMITTED_DATASET

SECRET_START = "2026-11-12T10:00:00"
SECRET_SPLIT = "비밀분할값"


def _task(**over: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "t-1",
        "scenario_id": "s-1",
        "cluster_id": "c-1",
        "split": "dev",
        "as_of_time": "2026-11-11T09:00:00+09:00",
        "account_id": "acct-x",
        "turns": [{"role": "user", "content": "질문"}],
        "gold": {"expected_answer": {"kind": "time_slot", "start": "2026-11-12T10:00:00+09:00"}},
    }
    task.update(over)
    return task


CASES = {
    "naive_start": _task(gold={"expected_answer": {"kind": "time_slot", "start": SECRET_START}}),
    "bad_split": _task(split=SECRET_SPLIT),
    "empty_turns": _task(turns=[]),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_invalid_task_raises_dataset_error_without_input_values(name: str, tmp_path: Path) -> None:
    root = tmp_path / "ds"
    root.mkdir()
    for f in ("manifest.json", "world.json"):
        shutil.copy(COMMITTED_DATASET / f, root / f)
    (root / "tasks.jsonl").write_text(json.dumps(CASES[name]) + "\n", encoding="utf-8")
    with pytest.raises(DatasetError) as info:
        load_dataset(root)
    message = str(info.value)
    assert "tasks.jsonl 1번째 줄" in message
    assert SECRET_START not in message and SECRET_SPLIT not in message
