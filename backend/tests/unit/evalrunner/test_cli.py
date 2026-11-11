import json
from pathlib import Path

import pytest

from orbit.evalrunner.cli import SEAL_FLAG, main

from .conftest import DATASET_DIR, good_script


def _script_file(tmp_path: Path) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(good_script(), ensure_ascii=False), encoding="utf-8")
    return path


def _run_args(tmp_path: Path, out: str, *extra: str) -> list[str]:
    return [
        "run", "--dataset", str(DATASET_DIR), "--split", "dev", "--sut", "scripted_tool",
        "--repeats", "2", "--out", str(tmp_path / out), "--llm-script", str(_script_file(tmp_path)),
        *extra,
    ]  # fmt: skip


def test_run_then_stats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "a.jsonl", "--seed", "1")) == 0
    lines = (tmp_path / "a.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8
    capsys.readouterr()

    assert main(_run_args(tmp_path, "b.jsonl", "--seed", "1", "--sut", "noop")) == 0
    capsys.readouterr()
    assert main(["stats", str(tmp_path / "a.jsonl"), "--compare", str(tmp_path / "b.jsonl")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["success_rate"]["rate"] == 1.0
    assert report["paired_difference_a_minus_b"]["difference"] > 0
    assert report["pass_k"]["k"] == 2


def test_test_split_requires_explicit_flag(tmp_path: Path) -> None:
    args = [
        "run", "--dataset", str(DATASET_DIR), "--split", "test", "--sut", "noop",
        "--out", str(tmp_path / "t.jsonl"),
    ]  # fmt: skip
    assert main(args) == 2
    assert not (tmp_path / "t.jsonl").exists()
    assert main([*args, SEAL_FLAG, "--repeats", "1"]) == 0
    assert len((tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    out = tmp_path / "x.jsonl"
    out.write_text("keep", encoding="utf-8")
    assert main(_run_args(tmp_path, "x.jsonl")) == 2
    assert out.read_text(encoding="utf-8") == "keep"


def test_estimate_writes_separate_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "main.jsonl", "--estimate", "2", "--repeats", "3")) == 0
    out = capsys.readouterr().out
    assert '"estimated_episodes": 12' in out
    assert not (tmp_path / "main.jsonl").exists()
    assert len((tmp_path / "main.estimate.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_cost_cap_exit_code(tmp_path: Path) -> None:
    assert main(_run_args(tmp_path, "c.jsonl", "--max-cost-usd", "0.0025", "--repeats", "3")) == 3
    assert len((tmp_path / "c.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_unknown_sut_is_usage_error(tmp_path: Path) -> None:
    assert main(_run_args(tmp_path, "u.jsonl", "--sut", "nope")) == 2
