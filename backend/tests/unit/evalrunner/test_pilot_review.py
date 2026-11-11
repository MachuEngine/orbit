"""파일럿 준비 리뷰 1회차: provider_items 전달, 모델 비교, 응답 모델 집합, length 비율."""

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner import stats
from orbit.evalrunner.cli import main
from orbit.evalrunner.llm_script import SCRIPTED_MODEL_ID, build_response
from orbit.evalrunner.runner import estimate_experiment, run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import Dataset
from orbit.llm.scripted import ScriptedLLM
from orbit.llm.types import LLMResponse, LLMUsage

from .conftest import DATASET_DIR, good_script

ITEM = {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC"}


def _factory(
    script: dict[str, list[dict[str, Any]]],
    *,
    patch: Callable[[LLMResponse], LLMResponse] = lambda r: r,
    sink: list[ScriptedLLM] | None = None,
) -> Callable[[Any, int], ScriptedLLM]:
    def make(task: Any, repeat: int) -> ScriptedLLM:
        llm = ScriptedLLM([patch(build_response(i)) for i in script.get(task.task_id, [])])
        if sink is not None:
            sink.append(llm)
        return llm

    return make


async def test_scripted_tool_sut_copies_provider_items(dataset: Dataset) -> None:
    llms: list[ScriptedLLM] = []
    factory = _factory(
        good_script(),
        patch=lambda r: dataclasses.replace(r, provider_items=(ITEM,) if r.tool_calls else ()),
        sink=llms,
    )
    await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut("scripted_tool"),
        llm_factory=factory,
        repeats=1,
        model_id=SCRIPTED_MODEL_ID,
        git_sha="t",
    )
    carried = [
        m
        for llm in llms
        for req in llm.requests
        for m in req.messages
        if m.role == "assistant" and m.tool_calls
    ]
    assert carried
    assert all(m.provider_items == (ITEM,) for m in carried)


async def test_assistant_v0_copies_provider_items(dataset: Dataset) -> None:
    llms: list[ScriptedLLM] = []
    factory = _factory(
        good_script(),
        patch=lambda r: dataclasses.replace(r, provider_items=(ITEM,)),
        sink=llms,
    )
    await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut("assistant_v0"),
        llm_factory=factory,
        repeats=1,
        model_id=SCRIPTED_MODEL_ID,
        git_sha="t",
    )
    carried = [
        m
        for llm in llms
        for req in llm.requests
        for m in req.messages
        if m.role == "assistant" and m.tool_calls
    ]
    assert carried
    assert all(m.provider_items == (ITEM,) for m in carried)


async def test_dated_response_model_is_not_mismatch_and_is_recorded(dataset: Dataset) -> None:
    dated = SCRIPTED_MODEL_ID + "-2026-11-01"
    summary = await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut("scripted_tool"),
        llm_factory=_factory(good_script(), patch=lambda r: dataclasses.replace(r, model_id=dated)),
        repeats=1,
        model_id=SCRIPTED_MODEL_ID,
        git_sha="t",
    )
    called = [r for r in summary.results if r.llm_calls]
    assert called
    assert all(r.models == (dated,) and not r.model_mismatch for r in called)

    other = await run_experiment(
        dataset,
        split="dev",
        sut_factory=lambda: create_sut("scripted_tool"),
        llm_factory=_factory(
            good_script(), patch=lambda r: dataclasses.replace(r, model_id="scripted-model-mini")
        ),
        repeats=1,
        model_id=SCRIPTED_MODEL_ID,
        git_sha="t",
    )
    assert all(r.model_mismatch for r in other.results if r.llm_calls)


async def test_estimate_reports_length_finish_rate(dataset: Dataset) -> None:
    est, summary = await estimate_experiment(
        dataset,
        split="dev",
        n=2,
        repeats=3,
        model_id=SCRIPTED_MODEL_ID,
        sut_factory=lambda: create_sut("scripted_tool"),
        llm_factory=_factory(
            good_script(),
            patch=lambda r: dataclasses.replace(r, finish_reason="length"),
        ),
        git_sha="t",
    )
    calls = sum(r.llm_calls for r in summary.results)
    assert calls > 0
    assert est.length_finish_rate == 1.0
    assert est.to_dict()["length_finish_rate"] == 1.0

    est2, _ = await estimate_experiment(
        dataset,
        split="dev",
        n=2,
        repeats=3,
        model_id=SCRIPTED_MODEL_ID,
        sut_factory=lambda: create_sut("scripted_tool"),
        llm_factory=_factory(good_script()),
        git_sha="t",
    )
    assert est2.length_finish_rate == 0.0


def _row(models: list[str]) -> dict[str, Any]:
    return {"usage": {"models": models}}


def test_response_model_set() -> None:
    one = stats.response_model_set([_row(["a-1"]), _row(["a-1"]), _row([])])
    assert one == {"models": ["a-1"], "single": True}
    many = stats.response_model_set([_row(["a-1"]), _row(["a-1-2026-11-01"])])
    assert many["single"] is False and many["models"] == ["a-1", "a-1-2026-11-01"]


def test_stats_cli_warns_when_models_differ(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "r.jsonl"
    rows = []
    for i, model in enumerate(["m-1", "m-2"], start=1):
        rows.append(
            {
                "task_id": "t-001",
                "repeat": i,
                "cluster_id": "c",
                "config_fingerprint": "fp",
                "config": {"dataset": "d@1", "split": "dev", "repeats": 2},
                "outcome": {"success": True, "failure_type": None},
                "usage": {"models": [model]},
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert main(["stats", str(path), "--include-incomplete"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["response_models"]["single"] is False
    assert report["warnings"]
    assert "경고" in captured.err


def _with_usage(r: LLMResponse) -> LLMResponse:
    return dataclasses.replace(r, usage=LLMUsage(100, 40, 30, 25))


async def test_usage_records_cached_and_reasoning_tokens(dataset: Dataset) -> None:
    est, summary = await estimate_experiment(
        dataset,
        split="dev",
        n=2,
        repeats=1,
        model_id=SCRIPTED_MODEL_ID,
        sut_factory=lambda: create_sut("scripted_tool"),
        llm_factory=_factory(good_script(), patch=_with_usage),
        git_sha="t",
    )
    for r in summary.results:
        row = json.loads(r.to_json_line())["usage"]
        assert row["cached_input_tokens"] == 30 * r.llm_calls
        assert row["reasoning_tokens"] == 25 * r.llm_calls
        assert row["output_tokens"] == 40 * r.llm_calls
    calls = sum(r.llm_calls for r in summary.results)
    assert est.mean_reasoning_tokens == 25 * calls / len(summary.results)
    assert est.to_dict()["mean_reasoning_tokens"] == est.mean_reasoning_tokens


def test_out_parent_directories_are_created(tmp_path: Path) -> None:
    out = tmp_path / "no" / "such" / "dir" / "r.jsonl"
    args = [
        "run", "--dataset", str(DATASET_DIR), "--split", "dev", "--sut", "noop",
        "--repeats", "1", "--out", str(out),
    ]  # fmt: skip
    assert main(args) == 0
    assert out.exists()
    est_out = tmp_path / "other" / "deep" / "e.jsonl"
    args[-1] = str(est_out)
    assert main([*args, "--estimate", "1"]) == 0
    assert (est_out.parent / "e.estimate.jsonl").exists()
    assert not est_out.exists()
