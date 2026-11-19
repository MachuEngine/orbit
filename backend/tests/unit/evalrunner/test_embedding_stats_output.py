"""가짜 임베딩으로 색인 준비 이후 질의 비용까지 최종 관측하는 CLI 회귀 테스트."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner import cli
from orbit.evalrunner.retrieval.embed_cache import CachingEmbedder
from orbit.evalrunner.runflow_index import EvalIndexProvider
from orbit.evalrunner.runner import ExperimentSummary
from orbit.mailindex.limits import EMBEDDING_DIM

from .conftest import DATASET_DIR


class CountingEmbedder:
    model_id = "synthetic-embedding"

    def __init__(self) -> None:
        self.usage_tokens = 0
        self.api_calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.usage_tokens += sum(len(text) for text in texts)
        self.api_calls += 1
        return [[0.0] * EMBEDDING_DIM for _ in texts]


def args(tmp_path: Path, name: str, *extra: str) -> list[str]:
    return [
        "run",
        "--dataset",
        str(DATASET_DIR),
        "--split",
        "dev",
        "--sut",
        "runflow_v0",
        "--index",
        "on",
        "--embedder",
        "fake",
        "--repeats",
        "1",
        "--out",
        str(tmp_path / f"{name}.jsonl"),
        *extra,
    ]


def install_fake_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    error: BaseException | None = None,
    build_error: bool = False,
) -> tuple[CachingEmbedder, list[dict[str, Any]]]:
    embedder = CachingEmbedder(CountingEmbedder(), tmp_path / "cache", price_per_mtok=1.0)
    captured: list[dict[str, Any]] = []
    database = object()

    def setup(namespace: Any, dataset: Any) -> cli._IndexSetup:
        return cli._IndexSetup(
            namespace,
            [],
            dataset.world_snapshot,
            "same-world",
            embedder.model_id,
            tmp_path / "cache",
            embedder=embedder,
        )

    async def build(setup: cli._IndexSetup) -> tuple[EvalIndexProvider, float]:
        assert setup.embedder is embedder
        if build_error:
            await embedder.embed(["synthetic-index-text"])
            assert error is not None
            raise error
        if not captured:
            assert embedder.stats()["cost_usd"] == 0  # 재사용 색인은 준비 중 API 비용이 없다.
        return EvalIndexProvider({}, embedder), 0.0

    async def run(dataset: Any, **kwargs: Any) -> ExperimentSummary:
        sut = kwargs["sut_factory"]()
        captured.append(
            {
                "version": sut.version,
                "config": sut.config_info(model_id="same-model"),
                "common": {
                    key: value
                    for key, value in kwargs.items()
                    if key not in {"llm_factory", "sut_factory", "sink"}
                },
            }
        )
        await embedder.embed(["synthetic-query-text"])
        if error is not None:
            raise error
        return ExperimentSummary("synthetic", "synthetic-fingerprint", 0)

    monkeypatch.setattr(cli, "_index_setup", setup)
    monkeypatch.setattr(cli._IndexSetup, "build", build)
    monkeypatch.setattr(cli, "_database_env", lambda _sut: database)
    # runflow_v0의 구성은 유지하고 실제 DB/모델 구동만 비동기 대역으로 대체한다.
    monkeypatch.setattr(cli, "run_experiment", run)
    monkeypatch.setattr(cli, "_git_state", lambda _out: ("same-git", False))
    return embedder, captured


def test_final_snapshot_includes_query_cost_after_zero_cost_index_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, _ = install_fake_run(monkeypatch, tmp_path)
    output = tmp_path / "nested" / "embedding.json"
    assert cli.main(args(tmp_path, "normal", "--embedding-stats-out", str(output))) == 0
    saved = json.loads(output.read_text())
    assert saved == embedder.stats()
    assert saved["cost_usd"] > 0 and saved["actual_tokens"] == len("synthetic-query-text")
    assert saved["api_calls"] == 1 and saved["cache_misses"] == 1
    assert "synthetic-query-text" not in output.read_text()
    assert list(output.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("during_build", [False, True])
def test_exception_exit_still_saves_observed_embedding_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    during_build: bool,
) -> None:
    original = RuntimeError("synthetic-runtime-failure")
    embedder, _ = install_fake_run(monkeypatch, tmp_path, error=original, build_error=during_build)
    output = tmp_path / "failed-embedding.json"
    with pytest.raises(RuntimeError) as caught:
        cli.main(args(tmp_path, "failed", "--embedding-stats-out", str(output)))
    assert caught.value is original
    assert json.loads(output.read_text()) == embedder.stats()
    assert json.loads(output.read_text())["cost_usd"] > 0


def test_snapshot_write_failure_does_not_mask_original_runtime_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = RuntimeError("synthetic-runtime-failure")
    install_fake_run(monkeypatch, tmp_path, error=original)

    def failed_write(*_args: Any) -> None:
        raise OSError("PRIVATE-WRITE-DETAIL")

    monkeypatch.setattr(cli, "_write_embedding_stats", failed_write)
    output = tmp_path / "unobserved.json"
    with pytest.raises(RuntimeError) as caught:
        cli.main(args(tmp_path, "failed-save", "--embedding-stats-out", str(output)))
    assert caught.value is original and not output.exists()
    stderr = capsys.readouterr().err
    assert "비용 미관측" in stderr and "PRIVATE-WRITE-DETAIL" not in stderr


def test_existing_snapshot_is_rejected_before_live_settings_or_embedding_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: Any) -> Any:
        raise AssertionError("사전 검증 뒤 호출되면 안 된다")

    monkeypatch.setattr(cli, "_index_setup", unexpected)
    monkeypatch.setattr(cli, "_live_llm", unexpected)
    output = tmp_path / "keep.json"
    output.write_text("keep")
    assert (
        cli.main(args(tmp_path, "existing", "--live-llm", "--embedding-stats-out", str(output)))
        == 2
    )
    assert output.read_text() == "keep" and not (tmp_path / "existing.jsonl").exists()


def test_index_off_and_dry_run_refuse_snapshot_option(tmp_path: Path) -> None:
    output = tmp_path / "not-written.json"
    assert (
        cli.main(args(tmp_path, "off", "--index", "off", "--embedding-stats-out", str(output))) == 2
    )
    assert (
        cli.main(args(tmp_path, "dry", "--index-dry-run", "--embedding-stats-out", str(output)))
        == 2
    )
    assert not output.exists()


def test_atomic_snapshot_refuses_a_target_created_after_preflight(
    tmp_path: Path,
) -> None:
    embedder = CachingEmbedder(CountingEmbedder(), tmp_path / "cache", price_per_mtok=1.0)
    output = tmp_path / "raced.json"
    output.write_text("other-writer")
    with pytest.raises(FileExistsError):
        cli._write_embedding_stats(output, embedder)
    assert output.read_text() == "other-writer"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_flag_does_not_change_sut_version_requests_or_config_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, captured = install_fake_run(monkeypatch, tmp_path)
    assert cli.main(args(tmp_path, "without")) == 0
    assert (
        cli.main(args(tmp_path, "with", "--embedding-stats-out", str(tmp_path / "stats.json"))) == 0
    )
    assert captured[0] == captured[1]


def test_parent_write_probe_fails_before_evaluation_or_live_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probed: list[Path] = []

    def denied_probe(*_args: Any, **kwargs: Any) -> Any:
        probed.append(Path(kwargs["dir"]))
        raise PermissionError("PRIVATE-PARENT-DETAIL")

    def unexpected(*_args: Any) -> Any:
        raise AssertionError("읽기 전용 부모라면 평가 전에 거절해야 한다")

    monkeypatch.setattr("orbit.evalrunner.cli.tempfile.TemporaryFile", denied_probe)
    monkeypatch.setattr(cli, "_run_evaluation", unexpected)
    output = tmp_path / "read-only-parent" / "stats.json"
    assert (
        cli.main(args(tmp_path, "blocked", "--live-llm", "--embedding-stats-out", str(output))) == 2
    )
    assert probed == [output.parent] and not output.exists()
    assert not (tmp_path / "blocked.jsonl").exists()
    assert "PRIVATE-PARENT-DETAIL" not in capsys.readouterr().err
