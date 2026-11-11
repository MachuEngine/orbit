import json
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner.cli import SEAL_FLAG, main
from orbit.llm.types import LLMResponse, LLMUsage

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
    assert main(_run_args(tmp_path, "c.jsonl", "--max-cost-usd", "0.0005", "--repeats", "3")) == 3
    assert len((tmp_path / "c.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_unknown_sut_is_usage_error(tmp_path: Path) -> None:
    assert main(_run_args(tmp_path, "u.jsonl", "--sut", "nope")) == 2


def test_error_output_does_not_echo_input_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "ds"
    root.mkdir()
    (root / "tasks.jsonl").write_text((DATASET_DIR / "tasks.jsonl").read_text("utf-8"), "utf-8")
    manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["counts"].update(messages=0, events=0)  # 검증은 통과하고 Sim 로드에서 실패하게 한다
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    world = {
        "schema_version": 1,
        "seed": "SECRET-MARKER-XYZ",
        "accounts": {"acct-alice": {}, "acct-bob": {}},
    }
    (root / "world.json").write_text(json.dumps(world), encoding="utf-8")
    args = ["run", "--dataset", str(root), "--split", "dev", "--sut", "noop"]
    assert main([*args, "--out", str(tmp_path / "o.jsonl")]) == 2
    err = capsys.readouterr().err
    assert "SECRET-MARKER-XYZ" not in err
    assert "DatasetError" in err


class _FixedTextLLM:
    async def complete(self, request: Any) -> LLMResponse:
        return LLMResponse(
            text="합성 답변",
            tool_calls=(),
            finish_reason="stop",
            usage=LLMUsage(10, 5),
            cost_usd=0.001,
            latency_ms=0,
            model_id="openai/gpt-6-luna",
            provider="openai",
        )


def _live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ORBIT_LLM_MODEL_ID", "openai/gpt-6-luna")
    monkeypatch.setenv("ORBIT_OPENAI_API_KEY", "sk-test-SYNTHETIC")  # gitleaks:allow
    monkeypatch.setenv("ORBIT_LLM_REASONING_EFFORT", "low")
    monkeypatch.delenv("ORBIT_LLM_PROVIDER_ORDER", raising=False)


def _live_args(tmp_path: Path, out: str, *extra: str) -> list[str]:
    return [
        "run", "--dataset", str(DATASET_DIR), "--split", "dev", "--sut", "assistant_v0",
        "--out", str(tmp_path / out), "--live-llm", *extra,
    ]  # fmt: skip


def test_live_llm_records_provider_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _live_env(monkeypatch)
    monkeypatch.setattr("orbit.evalrunner.cli.build_llm", lambda settings: _FixedTextLLM())
    args = _live_args(tmp_path, "l.jsonl", "--estimate", "2", "--max-cost-usd", "1")
    assert main(args) == 0
    assert '"estimated_episodes"' in capsys.readouterr().out
    lines = (tmp_path / "l.estimate.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    config = json.loads(lines[0])["config"]
    assert config["model_id"] == "openai/gpt-6-luna"
    assert config["llm"] == {
        "provider": "openai",
        "reasoning_effort": "low",
        "temperature_sent": False,
        "tool_strict": False,
        "reasoning_items_roundtrip": True,
        "accepted_response_models": [],
        "price_input_per_mtok": None,
        "price_cached_input_per_mtok": None,
        "price_output_per_mtok": None,
        "price_source": None,
    }
    assert config["experiment_budget"]["max_cost_usd"] == 1


def test_live_llm_rejects_bad_settings_without_echoing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _live_env(monkeypatch)
    monkeypatch.setenv("ORBIT_LLM_REASONING_EFFORT", "sk-test-SYNTHETIC-bad")  # gitleaks:allow
    assert main(_live_args(tmp_path, "b.jsonl")) == 2
    err = capsys.readouterr().err
    assert "llm_reasoning_effort" in err
    assert "SYNTHETIC" not in err
    assert not (tmp_path / "b.jsonl").exists()
    monkeypatch.setenv("ORBIT_LLM_REASONING_EFFORT", "low")
    assert main(_live_args(tmp_path, "b.jsonl", "--model-id", "openai/other-2")) == 2
    assert main([*_live_args(tmp_path, "b.jsonl"), "--llm-script", "x.json"]) == 2
