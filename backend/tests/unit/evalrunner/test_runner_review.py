"""1회차 리뷰 지적에 대한 회귀 테스트: 정답 격리, SUT 권한, 모델 기록, 취소, 순서, 우선순위."""

import asyncio
import dataclasses
import json
from dataclasses import fields
from typing import Any

import pytest

from orbit.connectors.contracts import EventDraft
from orbit.core.clock import FixedClock
from orbit.evalrunner.episode import Budget, Episode, EpisodeConnectors, EpisodeOutput
from orbit.evalrunner.llm_script import SCRIPTED_MODEL_ID
from orbit.evalrunner.runner import ExperimentBudget, run_experiment
from orbit.evalrunner.suts import ScriptedToolSUT
from orbit.evalrunner.task_source import Dataset
from orbit.llm.types import LLMRequest, LLMResponse

from .conftest import run

# ---------------------------------------------------------------- 정답 격리·권한


class Capture:
    name = "capture"
    version = "0"

    def __init__(self, sink: list[Episode]) -> None:
        self._sink = sink

    async def run(self, episode: Episode) -> EpisodeOutput:
        self._sink.append(episode)
        return EpisodeOutput(final_text="안녕")


async def _capture(dataset: Dataset) -> list[Episode]:
    seen: list[Episode] = []
    await run(dataset, repeats=1, sut_factory=lambda: Capture(seen))
    return seen


async def test_sut_receives_task_without_gold(dataset: Dataset) -> None:
    seen = await _capture(dataset)
    assert len(seen) == 4
    for ep in seen:
        assert {f.name for f in fields(ep.task)} == {"task_id", "turns", "as_of_time", "account_id"}
        for attr in (
            "gold", "forbidden_tools", "required_tool_calls", "must_not_contain",
            "answer_facts", "expected_changes", "scenario_id", "split", "difficulty", "tags",
        ):  # fmt: skip
            assert not hasattr(ep.task, attr)
        assert not any(hasattr(t, "gold") for t in ep.task.turns)
        exposed = json.dumps(dataclasses.asdict(ep.task), default=str, ensure_ascii=False)
        exposed += repr(ep.task) + json.dumps(vars(ep.task), default=str, ensure_ascii=False)
        for secret in (
            "cancel_event", "create_draft", "list_events", "search_messages", "팀 회고",
            "주간 회의", "any_of", "args_subset", "cal-alice-team",
        ):  # fmt: skip
            assert secret not in exposed
    assert ep.task.turns[0].content  # 질문은 그대로 전달된다


async def test_sut_authority_is_narrow(dataset: Dataset) -> None:
    ep = (await _capture(dataset))[0]
    assert {f.name for f in fields(Episode)} == {
        "task", "connectors", "clock", "llm", "budget", "steps",
    }  # fmt: skip
    assert {f.name for f in fields(EpisodeConnectors)} == {"mail", "calendar"}
    for attr in ("runtime", "calls", "fault_plan", "faults", "world"):
        assert not hasattr(ep, attr)
        assert not hasattr(ep.connectors, attr)
    assert not isinstance(ep.clock, FixedClock)
    assert not hasattr(ep.clock, "set") and not hasattr(ep.clock, "advance")
    assert ep.clock.now() == ep.task.as_of_time


# ---------------------------------------------------------------- 우선순위·출력 검증


def _no_forbidden(dataset: Dataset) -> Dataset:
    base = dataset.tasks_for("dev")[0]
    gold = base.gold.model_copy(update={"forbidden_tools": [], "required_tool_calls": []})
    task = base.model_copy(update={"gold": gold})
    return Dataset(dataset.name, dataset.version, dataset.manifest, dataset.world_snapshot, (task,))


async def test_unexpected_state_change_is_not_hidden_by_sut_error(dataset: Dataset) -> None:
    class WriteThenCrash:
        name = "write-then-crash"
        version = "0"

        async def run(self, episode: Episode) -> EpisodeOutput:
            draft = EventDraft(
                title="신규",
                start=episode.clock.now(),
                end=episode.clock.now().replace(hour=23),
            )
            await episode.connectors.calendar.create_event("cal-alice-main", "evabc001", draft)
            raise RuntimeError("중간에 실패")

    summary = await run(_no_forbidden(dataset), repeats=1, sut_factory=WriteThenCrash)
    r = summary.results[0]
    assert (r.success, r.failure_type) == (False, "unexpected_state_change")
    assert r.state_diff_match is False


async def test_non_str_final_text_is_sut_error_and_experiment_continues(dataset: Dataset) -> None:
    class BadOutput:
        name = "bad"
        version = "0"

        async def run(self, episode: Episode) -> EpisodeOutput:
            return EpisodeOutput(final_text=123)  # type: ignore[arg-type]

    summary = await run(dataset, repeats=1, sut_factory=BadOutput)
    assert summary.completed and len(summary.results) == 4
    assert all(
        (r.failure_type, r.detail) == ("sut_error", "InvalidOutput") for r in summary.results
    )


# ---------------------------------------------------------------- 모델 기록


async def test_actual_models_are_recorded_and_mismatch_flagged(dataset: Dataset) -> None:
    ok = await run(dataset, repeats=1, model_id=SCRIPTED_MODEL_ID)
    r = next(x for x in ok.results if x.task_id == "t-001")
    assert (r.models, r.providers, r.model_mismatch) == ((SCRIPTED_MODEL_ID,), (), False)
    bad = await run(dataset, repeats=1, model_id="declared-model")
    assert all(x.model_mismatch for x in bad.results if x.llm_calls)
    row = json.loads(bad.results[0].to_json_line())
    assert row["usage"]["models"] == [SCRIPTED_MODEL_ID] and row["model_mismatch"] is True
    unspecified = await run(dataset, repeats=1)  # ScriptedLLM이므로 선언 없이도 실행된다
    assert not any(x.model_mismatch for x in unspecified.results)


class SlowLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(5)
        raise AssertionError("도달하면 안 된다")


async def test_real_llm_requires_declared_model_id(dataset: Dataset) -> None:
    with pytest.raises(ValueError, match="model-id"):
        await run_experiment(
            dataset,
            split="dev",
            sut_factory=ScriptedToolSUT,
            llm_factory=lambda t, r: SlowLLM(),
            repeats=1,
        )


# ---------------------------------------------------------------- 취소된 호출


async def _run_slow(dataset: Dataset, cap: ExperimentBudget) -> Any:
    return await run_experiment(
        dataset,
        split="dev",
        sut_factory=ScriptedToolSUT,
        llm_factory=lambda t, r: SlowLLM(),
        repeats=1,
        model_id="slow-model",
        budget=Budget(max_seconds=0.05),
        experiment_budget=cap,
    )


async def test_cancelled_llm_call_makes_cost_unknown(dataset: Dataset) -> None:
    summary = await _run_slow(dataset, ExperimentBudget(max_cost_usd=1.0))
    first = summary.results[0]
    assert (first.failure_type, first.detail) == ("budget_exceeded", "time")
    assert first.llm_calls == 1  # 취소된 호출도 기록기에 남는다
    assert first.cost_usd is None
    # 비용이 불명인데 토큰 상한이 없으면 상한을 강제할 수 없으므로 중단한다
    assert summary.stopped_reason == "cost_unknown_no_token_cap"
    assert len(summary.results) == 1


async def test_cancelled_llm_call_falls_back_to_token_cap(dataset: Dataset) -> None:
    summary = await _run_slow(dataset, ExperimentBudget(max_cost_usd=1.0, max_tokens=1000))
    assert summary.completed and len(summary.results) == 4
    assert all(r.cost_usd is None for r in summary.results)


# ---------------------------------------------------------------- 실행 순서


async def test_repeat_major_order_with_seeded_shuffle(dataset: Dataset) -> None:
    a = await run(dataset, repeats=2, seed=3)
    order = [(r.repeat, r.task_id) for r in a.results]
    assert [rep for rep, _ in order] == [1, 1, 1, 1, 2, 2, 2, 2]
    assert [t for _, t in order[:4]] == [t for _, t in order[4:]]
    b = await run(dataset, repeats=2, seed=3)
    assert order == [(r.repeat, r.task_id) for r in b.results]
    orders = {
        tuple(r.task_id for r in (await run(dataset, repeats=1, seed=s)).results) for s in range(8)
    }
    assert len(orders) > 1  # 시드가 순서를 바꾼다


async def test_partial_run_covers_every_task_once_before_second_repeat(dataset: Dataset) -> None:
    summary = await run(dataset, repeats=3, experiment_budget=ExperimentBudget(max_tokens=60))
    # 에피소드당 15~30토큰: 몇 개만 돌고 중단하지만 모두 1회차에서 나온다
    assert summary.stopped_reason == "token_cap"
    assert {r.repeat for r in summary.results} == {1}
    assert len({r.task_id for r in summary.results}) == len(summary.results)


async def test_model_id_requirement_does_not_instantiate_an_llm(dataset: Dataset) -> None:
    created: list[int] = []

    def factory(task: Any, repeat: int) -> Any:
        created.append(1)
        return SlowLLM()

    with pytest.raises(ValueError, match="model-id"):
        await run_experiment(
            dataset, split="dev", sut_factory=ScriptedToolSUT, llm_factory=factory, repeats=1
        )
    assert created == []
