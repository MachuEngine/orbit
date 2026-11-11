"""커밋된 데이터셋(`eval/datasets/orbit-eval/v0`)을 러너로 끝까지 돌려 보는 스모크 테스트."""

import json
from collections import Counter
from pathlib import Path

import pytest

from orbit.evaldata.dataset import load_dataset as load_evaldata
from orbit.evalrunner.cli import SEAL_FLAG, main
from orbit.evalrunner.llm_script import scripted_llm_factory
from orbit.evalrunner.runner import SealedSplitError, run_experiment
from orbit.evalrunner.suts import NoopSUT
from orbit.evalrunner.task_source import load_dataset

DATASET = Path(__file__).resolve().parents[4] / "eval" / "datasets" / "orbit-eval" / "v0"


async def test_noop_sut_runs_every_dev_task_and_fails_with_answer_format() -> None:
    ds = load_dataset(DATASET)
    assert len(ds.tasks) == 60  # manifest.counts.tasks
    dev = ds.tasks_for("dev")
    assert 0 < len(dev) < 60
    summary = await run_experiment(
        ds,
        split="dev",
        sut_factory=NoopSUT,
        llm_factory=scripted_llm_factory({}),
        repeats=1,
        git_sha="smoke",
    )
    assert summary.completed and len(summary.results) == len(dev)
    assert {r.task_id for r in summary.results} == {t.task_id for t in dev}
    # 구조화된 답을 내지 않으므로 모든 과제가 answer_format으로 실패한다(채점이 끝까지 돈다)
    assert Counter(r.failure_type for r in summary.results) == {"answer_format": len(dev)}
    assert all(r.success is False and r.trajectory == () for r in summary.results)
    assert all(r.cluster_id.startswith("c-") for r in summary.results)
    assert all(r.state_diff_match is True for r in summary.results)  # 쓰기가 없다
    # 결과 행에 질문·정답 원문이 없다
    blob = "\n".join(r.to_json_line() for r in summary.results)
    for task in dev[:10]:
        assert task.turns[0].content not in blob


async def test_test_split_is_sealed_without_flag() -> None:
    ds = load_dataset(DATASET)
    with pytest.raises(SealedSplitError):
        await run_experiment(
            ds,
            split="test",
            sut_factory=NoopSUT,
            llm_factory=scripted_llm_factory({}),
            repeats=1,
        )


def test_cli_rejects_test_split_without_flag_and_runs_with_it(tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    args = ["run", "--dataset", str(DATASET), "--split", "test", "--sut", "noop", "--out", str(out)]
    assert main(args) == 2 and not out.exists()
    assert main([*args, "--repeats", "1", SEAL_FLAG]) == 0
    test_count = sum(t.split == "test" for t in load_evaldata(DATASET).tasks)
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == test_count
