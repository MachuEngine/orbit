from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner.llm_script import scripted_llm_factory
from orbit.evalrunner.runner import ExperimentSummary, run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import Dataset, load_dataset

DATASET_DIR = Path(__file__).parent / "fixtures" / "dataset"

Script = dict[str, list[dict[str, Any]]]


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def step(
    text: str | None = None,
    calls: list[dict[str, Any]] | None = None,
    *,
    cost: float | None = 0.001,
) -> dict[str, Any]:
    return {
        "text": text,
        "tool_calls": calls or [],
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": cost,
    }


def good_script(cost: float | None = 0.001) -> Script:
    """dev 과제 t-001·t-002·t-003·t-005에 모두 정답인 스크립트."""
    return {
        "t-001": [
            step(
                calls=[
                    call(
                        "list_events",
                        calendar_id="cal-alice-main",
                        time_min="2026-11-12T00:00:00+09:00",
                        time_max="2026-11-13T00:00:00+09:00",
                    )
                ],
                cost=cost,
            ),
            step("내일은 주간 회의와 점심이 있습니다.", cost=cost),
        ],
        "t-002": [
            step(
                calls=[
                    call(
                        "list_events",
                        calendar_id="cal-alice-team",
                        time_min="2026-11-13T00:00:00+09:00",
                        time_max="2026-11-14T00:00:00+09:00",
                    )
                ],
                cost=cost,
            ),
            {
                **step("모레는 팀 회고가 있습니다.", cost=cost),
                "structured": {
                    "kind": "event_set",
                    "events": [{"calendar_id": "cal-alice-team", "event_id": "evalice003"}],
                },
            },
        ],
        "t-003": [
            step(calls=[call("search_messages", query="from:bob@example.org")], cost=cost),
            step("제목은 프로젝트 일정 문의입니다.", cost=cost),
        ],
        "t-005": [step("안녕하세요", cost=cost)],
    }


@pytest.fixture
def dataset() -> Dataset:
    return load_dataset(DATASET_DIR)


async def run(
    dataset: Dataset,
    script: Mapping[str, list[dict[str, Any]]] | None = None,
    *,
    sut: str = "scripted_tool",
    split: str = "dev",
    **kwargs: Any,
) -> ExperimentSummary:
    kwargs.setdefault("sut_factory", lambda: create_sut(sut))
    return await run_experiment(
        dataset,
        split=split,
        llm_factory=scripted_llm_factory(script if script is not None else good_script()),
        git_sha="test-sha",
        **kwargs,
    )
