"""`runflow_v0 --index on`의 DB 없이 확인할 수 있는 부분 (ADR-0023 §8). 데이터는 합성 값이다."""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orbit.core.clock import FixedClock
from orbit.evalrunner.cli import main
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.runflow_index import NEVER_STALE, EvalIndexProvider
from orbit.evalrunner.suts import create_sut
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import FakeEmbedder
from orbit.mailindex.sync import sync_before_query
from orbit.runflow.prompt import PROMPT_VERSION

from .conftest import DATASET_DIR


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run", "--dataset", str(DATASET_DIR), "--split", "dev", "--sut", "runflow_v0",
        "--repeats", "1", "--out", str(tmp_path / "r.jsonl"), *extra,
    ]  # fmt: skip


def _eval_env(monkeypatch: pytest.MonkeyPatch, database: str) -> None:
    for name, role in (("EVAL_DATABASE", "orbit_app"), ("EVAL_MIGRATION_DATABASE", "orbit_owner")):
        url = f"postgresql+psycopg://{role}:pw@localhost:1/{database}"
        monkeypatch.setenv(f"ORBIT_{name}_URL", url)


def test_on_needs_a_provider_and_off_refuses_one() -> None:
    with pytest.raises(ValueError, match="색인 제공자"):
        create_sut("runflow_v0", index_mode="on")
    provider = EvalIndexProvider({}, FakeEmbedder())
    with pytest.raises(ValueError, match="색인 제공자"):
        create_sut("runflow_v0", index_mode="off", index=provider)


def test_off_config_is_unchanged() -> None:
    sut = create_sut("runflow_v0")
    assert sut.version == f"0+{PROMPT_VERSION}+eval-answer-v2+index-off"
    info = sut.config_info(model_id="m")  # type: ignore[attr-defined]
    assert set(info) == {
        "runflow_fingerprint",
        "answer_extension",
        "variants",
        "index_mode",
        "max_approvals_per_run",
    }


def test_on_config_records_mode_and_index_settings() -> None:
    sut = create_sut("runflow_v0", index_mode="on", index=EvalIndexProvider({}, FakeEmbedder()))
    assert sut.version.endswith("+index-on")
    info = sut.config_info(model_id="m")  # type: ignore[attr-defined]
    assert info["index_mode"] == "on"
    assert info["index"]["embedding_model_id"] == FakeEmbedder().model_id
    assert info["index"]["sync_before_query"] is False


async def test_query_time_sync_is_off() -> None:
    """색인 시각에서 1년이 지나도 질의 직전 동기화는 시도하지 않는다(고정 시점 스냅샷)."""
    clock = FixedClock(worldmail.INDEX_NOW + timedelta(days=400))
    mail = AsyncMock()  # 커넥터가 불리면 호출 기록이 남는다
    mi = MailIndexDeps(
        db=AsyncMock(),
        clock=clock,
        ids=AsyncMock(),
        embedder=FakeEmbedder(),
        query_stale=NEVER_STALE,
    )
    tried = await sync_before_query(mi, mail, AsyncMock(), AsyncMock(), worldmail.INDEX_NOW)
    assert tried is False
    assert mail.mock_calls == []


# ---------------------------------------------------------------- CLI


def test_cli_index_dry_run_estimates_without_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / "cache"
    args = _args(
        tmp_path, "--index", "on", "--index-dry-run", "--embedder", "fake",
        "--embed-cache-dir", str(cache),
    )  # fmt: skip
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["calls_made"] == 0
    assert report["embedding_model_id"] == FakeEmbedder().model_id
    assert set(report["accounts"]) == {"acct-alice"}  # 과제에 나오는 계정만
    assert report["accounts"]["acct-alice"]["mails"] > 0
    assert not cache.exists() and not (tmp_path / "r.jsonl").exists()  # 아무것도 만들지 않는다


@pytest.mark.parametrize(
    "extra",
    [("--index-dry-run",), ("--max-embed-cost-usd", "1")],  # 둘 다 --index on 없이
)
def test_cli_index_options_need_index_on(tmp_path: Path, extra: tuple[str, ...]) -> None:
    assert main(_args(tmp_path, *extra)) == 2
    assert not (tmp_path / "r.jsonl").exists()


def test_cli_index_on_is_only_for_runflow(tmp_path: Path) -> None:
    args = _args(tmp_path, "--index", "on")
    args[args.index("runflow_v0")] = "noop"
    assert main(args) == 2


def test_cli_index_on_refuses_non_eval_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _eval_env(monkeypatch, "orbit")
    assert main(_args(tmp_path, "--index", "on", "--embedder", "fake")) == 2
    err = capsys.readouterr().err
    assert "orbit_eval" in err and "pw@" not in err
    assert not (tmp_path / "r.jsonl").exists()


def test_cli_index_on_openai_needs_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _eval_env(monkeypatch, "orbit_eval")
    monkeypatch.delenv("ORBIT_OPENAI_API_KEY", raising=False)
    assert main(_args(tmp_path, "--index", "on", "--embed-cache-dir", str(tmp_path / "c"))) == 2
    assert "ORBIT_OPENAI_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "r.jsonl").exists()
