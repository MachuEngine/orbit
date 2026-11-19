"""합성 입력만 쓰는 평가 RAG 정책·CLI 경계 회귀."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orbit.evalrunner import cli
from orbit.evalrunner.llm_script import build_response
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.evalrunner.runner import run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import load_dataset
from orbit.llm.interface import LLMRequestError
from orbit.llm.scripted import ScriptedLLM
from orbit.records.runs import RunMessageRecord
from orbit.runflow.fingerprint import config_fingerprint
from orbit.runflow.rag import RagPolicy, initial_call_id, parsed_query
from orbit.runflow.transcript import Transcript, _wire_id

from .conftest import DATASET_DIR
from .test_embedding_stats_output import args, install_fake_run

INITIAL_CALL_ID = initial_call_id("initial")


def transcript(question: str) -> Transcript:
    return Transcript(
        (RunMessageRecord(1, "user", {"text": question}, datetime(2026, 1, 1, tzinfo=UTC)),)
    )


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "no json",
        "[]",
        "{}",
        '{"query": " "}',
        '{"query": 7}',
        '{"query": "ok", "other": 1}',
        json.dumps({"query": "a" * 501}),
    ],
)
def test_invalid_rewrite_falls_back_without_repair_call(text: str | None) -> None:
    assert parsed_query(text, "current question") == ("current question", True)


async def test_rewrite_is_one_bounded_call_and_invalid_falls_back() -> None:
    policy = RagPolicy("rewrite_retrieve_once")
    llm = ScriptedLLM([build_response({"text": "invalid"})])
    assert await policy.query(transcript("현재 질문"), llm) == "현재 질문"
    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert request.tools == () and request.max_output_tokens == 256
    assert request.temperature == 0 and request.metadata["step_kind"] == "rag_rewrite"
    assert policy.steps == [{"kind": "rag_rewrite", "fallback": True}]


@pytest.mark.parametrize("pattern", ["retrieve_once", "rewrite_retrieve_once"])
async def test_long_original_query_is_bounded_and_control_only_trace(pattern: str) -> None:
    policy = RagPolicy(pattern)
    assert (
        await policy.query(transcript("가" * 700), ScriptedLLM([build_response({"text": "bad"})]))
        == "가" * 500
    )
    assert policy.steps[-1] == {"kind": "rag_query_truncated", "source": "current_question"}
    assert "가" not in json.dumps(policy.steps, ensure_ascii=False)


async def test_transport_error_is_not_rewrite_fallback() -> None:
    policy = RagPolicy("rewrite_retrieve_once")
    llm = ScriptedLLM([LLMRequestError("synthetic")])
    with pytest.raises(LLMRequestError):
        await policy.query(transcript("질문"), llm)
    assert policy.steps == [{"kind": "rag_rewrite", "error_kind": "LLMRequestError"}]


def test_default_reserved_looking_call_id_and_fingerprint_are_unchanged() -> None:
    assert _wire_id(INITIAL_CALL_ID) == INITIAL_CALL_ID
    assert _wire_id(INITIAL_CALL_ID, rag_reserved=True) == f"model_{INITIAL_CALL_ID}"
    assert config_fingerprint() == config_fingerprint(rag_pattern="agentic")
    assert config_fingerprint() != config_fingerprint(rag_pattern="retrieve_once")
    plain, explicit = RunflowV0SUT(), RunflowV0SUT(rag_pattern="agentic")
    assert plain.version == explicit.version
    assert plain.config_info(model_id="synthetic") == explicit.config_info(model_id="synthetic")
    assert "rag_pattern" not in plain.config_info(model_id="synthetic")


def test_default_request_preserves_reserved_looking_model_call_id() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rows = (
        RunMessageRecord(
            1,
            "assistant",
            {
                "step": 0,
                "tool_calls": [
                    {"id": INITIAL_CALL_ID, "name": "search_mail", "arguments": {"query": "q"}}
                ],
            },
            now,
        ),
        RunMessageRecord(
            2,
            "tool",
            {"step": 0, "tool_call_id": INITIAL_CALL_ID, "name": "search_mail", "text": "{}"},
            now,
        ),
    )
    plain = Transcript(rows).llm_messages("system", "0123456789ab")
    assert plain[1].tool_calls[0].id == INITIAL_CALL_ID
    assert plain[2].tool_call_id == INITIAL_CALL_ID
    initial = RunMessageRecord(
        0,
        "assistant",
        {
            "step": -1,
            "origin": "rag_initial",
            "tool_calls": [
                {"id": INITIAL_CALL_ID, "name": "search_indexed_mail", "arguments": {"query": "q"}}
            ],
        },
        now,
    )
    opt_in = Transcript((initial, *rows)).llm_messages("system", "0123456789ab")
    assert opt_in[1].tool_calls[0].id == INITIAL_CALL_ID
    assert opt_in[2].tool_calls[0].id == f"model_{INITIAL_CALL_ID}"
    assert opt_in[3].tool_call_id == f"model_{INITIAL_CALL_ID}"


@pytest.mark.parametrize(
    "extra",
    [
        ["--sut", "noop"],
        ["--index", "off"],
        ["--pattern", "fixed_workflow"],
        ["--runflow-variant", "facts"],
        ["--model-id", "other"],
    ],
)
def test_invalid_cli_combination_fails_before_index_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
) -> None:
    def forbidden(*_args: Any) -> Any:
        pytest.fail("invalid options reached index setup")

    monkeypatch.setattr(cli, "_index_setup", forbidden)
    assert (
        cli.main(
            args(
                tmp_path,
                "bad",
                "--rag-pattern",
                "retrieve_once",
                "--model-id",
                "openai/gpt-6-luna",
                *extra,
            )
        )
        == 2
    )
    assert not (tmp_path / "bad.jsonl").exists()


def test_cli_passes_rag_opt_in_without_changing_agentic_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, captured = install_fake_run(monkeypatch, tmp_path)
    assert (
        cli.main(
            args(
                tmp_path, "rag", "--rag-pattern", "retrieve_once", "--model-id", "openai/gpt-6-luna"
            )
        )
        == 0
    )
    assert captured[0]["config"]["rag_pattern"] == "retrieve_once"
    assert captured[0]["version"].endswith("+rag-once-v1-retrieve_once")


async def test_mail_subset_selection_and_fingerprint_are_runner_only() -> None:
    original = load_dataset(DATASET_DIR)
    tasks = tuple(t.model_copy(update={"tags": ["scenario:mail_fact"]}) for t in original.tasks)
    data = replace(original, tasks=tasks)
    selected = cli._mail_content_subset(data)
    assert all(t.split == "dev" for t in selected.tasks)
    assert len(selected.tasks) < len(tasks)
    assert "evaluation_subset" not in data.manifest
    meta = selected.manifest["evaluation_subset"]
    assert len(meta["manifest_sha256"]) == 64
    assert meta["task_ids"] == sorted(t.task_id for t in selected.tasks)
    summary = await run_experiment(
        selected,
        split="dev",
        sut_factory=lambda: create_sut("noop"),
        llm_factory=lambda _task, _repeat: ScriptedLLM([]),
        model_id="synthetic",
        repeats=1,
    )
    assert summary.results[0].config["subset"] == meta
    assert summary.results[0].trace is not None and "subset" not in summary.results[0].trace


def test_nondefault_sut_rejects_index_off() -> None:
    with pytest.raises(ValueError):
        RunflowV0SUT(rag_pattern="retrieve_once")
    # index 제공자 조건은 기존 계약대로 유지된다.
    with pytest.raises(ValueError):
        RunflowV0SUT(index_mode="off", index=object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "model, effort", [("other", "none"), ("openai/gpt-6-luna", "high"), ("openai/gpt-6-luna", None)]
)
def test_invalid_live_model_or_reasoning_is_rejected_before_api_or_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    effort: str | None,
) -> None:
    monkeypatch.setattr(
        cli,
        "get_llm_settings",
        lambda: SimpleNamespace(llm_model_id=model, llm_reasoning_effort=effort),
    )

    def forbidden(*_args: Any) -> Any:
        pytest.fail("invalid live config reached an external setup")

    monkeypatch.setattr(cli, "_live_llm", forbidden)
    monkeypatch.setattr(cli, "_index_setup", forbidden)
    assert (
        cli.main(args(tmp_path, "bad-live", "--live-llm", "--rag-pattern", "rewrite_retrieve_once"))
        == 2
    )


def test_reviewed_mail_subset_is_21_tasks_17_clusters_and_five_multiturn() -> None:
    root = Path(__file__).resolve().parents[4]
    data = load_dataset(root / "eval/datasets/orbit-eval/v1")
    selected = cli._mail_content_subset(data)
    meta = selected.manifest["evaluation_subset"]
    assert len(selected.tasks) == 21 and len(meta["clusters"]) == 17
    assert meta["multiturn_tasks"] == 5
    changed = cli._mail_content_subset(
        replace(data, manifest={**data.manifest, "version": "different"})
    )
    assert changed.manifest["evaluation_subset"]["manifest_sha256"] != meta["manifest_sha256"]
