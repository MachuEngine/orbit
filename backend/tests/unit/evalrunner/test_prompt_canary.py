"""평가 전용 카나리: 실사용 지침에 없고, 켤 때만 지문에 들어간다."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from orbit.evaldata.safety_v1.constants import CANARY_SENTENCE, CANARY_TOKEN, PROMPT_CANARIES
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.runflow.prompt import build_system_prompt

SRC = Path(__file__).resolve().parents[3] / "src" / "orbit"


def test_live_prompt_has_no_canary() -> None:
    prompt = build_system_prompt(datetime(2026, 11, 12, tzinfo=UTC), UTC, "bnd")
    assert CANARY_TOKEN not in prompt


def test_live_code_never_imports_canary() -> None:
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).parts
        if rel[0] in ("evaldata", "evalrunner"):
            continue
        text = path.read_text(encoding="utf-8")
        assert not any(
            name in text for name in ("PROMPT_CANARIES", "CANARY_SENTENCE", CANARY_TOKEN)
        ), path


def test_default_fingerprint_has_no_canary_parts() -> None:
    sut = RunflowV0SUT()
    info = sut.config_info(model_id="m")
    assert "canary" not in sut.version
    assert not {"prompt_canary", "prompt_canary_sha"} & set(info)
    assert sut._combined_note() is None


def test_canary_changes_version_and_config() -> None:
    plain, sut = RunflowV0SUT(), RunflowV0SUT(prompt_canary="safety-v1")
    assert sut.version.startswith(plain.version) and "+canary-" in sut.version
    info = sut.config_info(model_id="m")
    assert info["prompt_canary"] == "safety-v1" and info["prompt_canary_sha"]
    assert info["runflow_fingerprint"] == plain.config_info(model_id="m")["runflow_fingerprint"]
    assert sut._combined_note() == CANARY_SENTENCE


def test_canary_joins_answer_note_after_it() -> None:
    sut = RunflowV0SUT(prompt_canary="safety-v1", answer_note="메모")
    assert sut._combined_note() == "메모\n" + PROMPT_CANARIES["safety-v1"]


def test_unknown_canary_rejected() -> None:
    with pytest.raises(ValueError):
        RunflowV0SUT(prompt_canary="nope")


# HEAD(카나리 도입 전) `_run_fingerprint`가 낸 값. `git show HEAD:`의 코드로 구했다.
HEAD_DEFAULT = "a963b8c011e868e890da54987aa9fcf721c57273ba11595a267c2ffcb73330ea"
HEAD_ADVANCED = "a616d521d916c24f4130cafc118eba886230f7f6ec256b93725e85a777f961fb"


class _Reranker:
    model_id, revision, max_length, device = "rr", "rev", 512, "cpu"


def test_run_fingerprint_without_canary_is_byte_identical_to_head() -> None:
    assert RunflowV0SUT()._run_fingerprint() == HEAD_DEFAULT
    assert RunflowV0SUT(answer_note="x")._run_fingerprint() == HEAD_DEFAULT
    advanced = RunflowV0SUT(
        index_mode="on",
        index=cast(Any, object()),
        search_variant="advanced",
        reranker=cast(Any, _Reranker()),
    )
    assert advanced._run_fingerprint() == HEAD_ADVANCED


def test_run_fingerprint_changes_with_canary() -> None:
    with_canary = RunflowV0SUT(prompt_canary="safety-v1")._run_fingerprint()
    assert with_canary != HEAD_DEFAULT
    assert (
        RunflowV0SUT(prompt_canary="safety-v1", answer_note="x")._run_fingerprint() != with_canary
    )


SAFETY_DS = Path(__file__).resolve().parents[4] / "eval" / "datasets" / "orbit-eval" / "safety-v1"


def test_cli_refuses_safety_set_on_other_sut(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from orbit.evalrunner.cli import main

    out = tmp_path / "x.jsonl"
    args = [
        "run",
        "--dataset",
        str(SAFETY_DS),
        "--split",
        "dev",
        "--sut",
        "noop",
        "--out",
        str(out),
    ]
    assert main(args) == 2
    err = capsys.readouterr().err
    assert "안전 세트는 runflow_v0에서만 실행한다" in err and "--prompt-canary" not in err
    assert not out.exists()


def test_cli_refuses_system_leak_dataset_without_canary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from orbit.evalrunner.cli import main

    monkeypatch.setenv("ORBIT_EVAL_DATABASE_URL", "postgresql://unused")
    out = tmp_path / "x.jsonl"
    args = ["run", "--dataset", str(SAFETY_DS), "--split", "dev", "--sut", "runflow_v0"]
    assert main([*args, "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "--prompt-canary safety-v1이 필요하다" in err
    assert not out.exists()
