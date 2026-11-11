"""오라클 스모크: 커밋된 데이터셋의 정답을 그대로 내면 모두 성공해야 한다.

데이터 형식과 구조화 채점이 맞물리는지 확인한다. 테스트 전용 SUT이며 레지스트리에 등록하지 않는다.
"""

from pathlib import Path
from typing import Any

from orbit.evalrunner.episode import Episode, EpisodeOutput
from orbit.evalrunner.task_source import Dataset, load_dataset

from .conftest import run

COMMITTED = Path(__file__).resolve().parents[4] / "eval" / "datasets" / "orbit-eval" / "v0"


class OracleSUT:
    name = "oracle"
    version = "0"

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self._answers = answers

    async def run(self, episode: Episode) -> EpisodeOutput:
        return EpisodeOutput(
            final_text="정답을 제출합니다.", structured=self._answers[episode.task.task_id]
        )


def oracle_answers(dataset: Dataset) -> dict[str, dict[str, Any]]:
    return {
        t.task_id: t.gold.expected_answer.model_dump(mode="json")
        for t in dataset.tasks
        if t.gold.expected_answer is not None
    }


async def test_oracle_succeeds_on_every_committed_dev_task() -> None:
    dataset = load_dataset(COMMITTED)
    dev = dataset.tasks_for("dev")
    answers = oracle_answers(dataset)
    assert dev and all(t.task_id in answers for t in dev)  # 모든 dev 과제에 구조화된 정답이 있다
    summary = await run(dataset, {}, repeats=1, sut_factory=lambda: OracleSUT(answers))
    assert len(summary.results) == len(dev)
    failed = [(r.task_id, r.failure_type) for r in summary.results if r.success is not True]
    assert failed == []
