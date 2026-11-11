import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from orbit.connectors.contracts import NotFound
from orbit.connectors.sim.faults import FaultPlan
from orbit.evalrunner.episode import Budget, Episode, EpisodeOutput
from orbit.evalrunner.llm_script import SCRIPTED_MODEL_ID, scripted_llm_factory
from orbit.evalrunner.runner import (
    EpisodeResult,
    ExperimentBudget,
    FaultPlanSpec,
    SealedSplitError,
    episode_seed,
    estimate_experiment,
    run_experiment,
)
from orbit.evalrunner.suts import NoopSUT, ScriptedToolSUT
from orbit.evalrunner.task_source import Dataset, GoldRecord, TaskRecord
from orbit.llm.types import LLMRequest, Message

from .conftest import call, good_script, run, step


def by_task(summary: Any, task_id: str) -> list[EpisodeResult]:
    return [r for r in summary.results if r.task_id == task_id]


async def test_all_correct_succeeds(dataset: Dataset) -> None:
    summary = await run(dataset, repeats=2)
    assert summary.completed
    assert len(summary.results) == 8  # dev 과제 4개 x 2회. test 과제는 실행하지 않는다
    assert all(r.success and r.failure_type is None for r in summary.results)
    t1 = by_task(summary, "t-001")[0]
    assert t1.trajectory == ("list_events",)
    assert (t1.llm_calls, t1.input_tokens, t1.output_tokens) == (2, 20, 10)
    assert t1.cost_usd == pytest.approx(0.002)
    assert t1.state_diff_match is True


async def test_nonempty_expected_changes_is_unscored_not_failed(dataset: Dataset) -> None:
    summary = await run(dataset, repeats=1)
    t5 = by_task(summary, "t-005")[0]
    assert t5.success
    assert t5.unscored == ("expected_changes",)
    assert t5.state_diff_match is None


async def test_wrong_answer_fails(dataset: Dataset) -> None:
    script = good_script()
    script["t-001"][1] = step("일정이 없습니다.")
    summary = await run(dataset, script, repeats=1)
    r = by_task(summary, "t-001")[0]
    assert (r.success, r.failure_type) == (False, "wrong_answer")


async def test_must_not_contain_fails(dataset: Dataset) -> None:
    script = good_script()
    script["t-001"][1] = step("주간 회의, 점심, 그리고 팀 회고가 있습니다.")
    summary = await run(dataset, script, repeats=1)
    assert by_task(summary, "t-001")[0].failure_type == "wrong_answer"


async def test_forbidden_tool_fails_and_wins_over_state_change(dataset: Dataset) -> None:
    script = good_script()
    script["t-001"] = [
        step(
            calls=[
                call(
                    "cancel_event",
                    calendar_id="cal-alice-main",
                    event_id="evalice001",
                    if_match="v-e1",
                )
            ]
        ),
        step("내일은 주간 회의와 점심이 있습니다."),
    ]
    summary = await run(dataset, script, repeats=1)
    r = by_task(summary, "t-001")[0]
    assert r.failure_type == "forbidden_tool"
    assert r.state_diff_match is False  # 상태 변화도 따로 기록된다
    assert r.trajectory == ("cancel_event",)


async def test_state_change_without_forbidden_tool_is_unexpected_state_change(
    dataset: Dataset,
) -> None:
    # 금지 도구 목록이 없는 과제를 만들어, 상태 변화만으로 실패하는 경로를 확인한다.
    base = dataset.tasks_for("dev")[0]
    assert isinstance(base, TaskRecord) and isinstance(base.gold, GoldRecord)
    task = replace(base, gold=replace(base.gold, forbidden_tools=(), required_tool_calls=()))
    ds = Dataset(dataset.name, dataset.version, dataset.manifest, dataset.world_snapshot, (task,))
    script = {
        "t-001": [
            step(
                calls=[
                    call(
                        "create_event",
                        calendar_id="cal-alice-main",
                        event_id="evabc001",
                        draft={
                            "title": "신규",
                            "start": "2026-11-14T01:00:00+00:00",
                            "end": "2026-11-14T02:00:00+00:00",
                        },
                    )
                ]
            ),
            step("주간 회의와 점심이 있습니다."),
        ]
    }
    summary = await run(ds, script, repeats=1)
    r = summary.results[0]
    assert r.failure_type == "unexpected_state_change"
    assert r.state_diff_match is False


async def test_missing_required_tool_call(dataset: Dataset) -> None:
    script = good_script()
    script["t-001"] = [step("내일은 주간 회의와 점심이 있습니다.")]
    summary = await run(dataset, script, repeats=1)
    assert by_task(summary, "t-001")[0].failure_type == "missing_tool_call"


async def test_required_call_args_mismatch_is_missing(dataset: Dataset) -> None:
    script = good_script()
    script["t-001"][0] = step(
        calls=[
            call(
                "list_events",
                calendar_id="cal-alice-team",  # 요구는 cal-alice-main
                time_min="2026-11-12T00:00:00+09:00",
                time_max="2026-11-13T00:00:00+09:00",
            )
        ]
    )
    summary = await run(dataset, script, repeats=1)
    assert by_task(summary, "t-001")[0].failure_type == "missing_tool_call"


async def test_noop_sut_fails_with_wrong_or_missing(dataset: Dataset) -> None:
    summary = await run(dataset, sut="noop", repeats=1)
    types = {r.task_id: r.failure_type for r in summary.results}
    assert types["t-001"] == "missing_tool_call"
    assert types["t-005"] == "wrong_answer"  # 고정 문장에 "안녕"이 없다
    assert all(r.llm_calls == 0 and r.trajectory == () for r in summary.results)


@pytest.mark.parametrize(
    ("budget", "reason"),
    [
        (Budget(max_steps=1), "steps"),
        (Budget(max_tokens=10), "tokens"),
    ],
)
async def test_budget_exceeded_by_counter(dataset: Dataset, budget: Budget, reason: str) -> None:
    summary = await run(dataset, repeats=1, budget=budget)
    r = by_task(summary, "t-001")[0]
    assert (r.success, r.failure_type, r.detail) == (False, "budget_exceeded", reason)


async def test_budget_is_rechecked_when_sut_skips_consume(dataset: Dataset) -> None:
    class Greedy:
        name = "greedy"
        version = "0"

        async def run(self, episode: Episode) -> EpisodeOutput:
            for _ in range(3):  # consume()을 부르지 않는다
                await episode.llm.complete(
                    LLMRequest(
                        messages=(Message(role="user", content="x"),), purpose="system_under_test"
                    )
                )
            return EpisodeOutput(final_text="안녕")

    script = {t: [step("a"), step("b"), step("c")] for t in ("t-001", "t-002", "t-003", "t-005")}
    summary = await run(dataset, script, repeats=1, sut_factory=Greedy, budget=Budget(max_steps=2))
    assert all(
        (r.failure_type, r.detail, r.llm_calls) == ("budget_exceeded", "steps", 3)
        for r in summary.results
    )


async def test_wall_clock_budget(dataset: Dataset) -> None:
    class Slow:
        name = "slow"
        version = "0"

        async def run(self, episode: Episode) -> EpisodeOutput:
            await asyncio.sleep(5)
            return EpisodeOutput(final_text="안녕")

    summary = await run(dataset, repeats=1, sut_factory=Slow, budget=Budget(max_seconds=0.05))
    assert all(r.failure_type == "budget_exceeded" and r.detail == "time" for r in summary.results)


async def test_sut_exception_is_recorded(dataset: Dataset) -> None:
    class Boom:
        name = "boom"
        version = "0"

        async def run(self, episode: Episode) -> EpisodeOutput:
            raise RuntimeError("비밀 질문 내용이 들어간 메시지")

    summary = await run(dataset, repeats=1, sut_factory=Boom)
    assert all((r.failure_type, r.detail) == ("sut_error", "RuntimeError") for r in summary.results)
    assert "비밀" not in json.dumps([r.to_dict() for r in summary.results], ensure_ascii=False)


async def test_unhandled_connector_error(dataset: Dataset) -> None:
    script = {
        "t-001": [
            step(calls=[call("get_event", calendar_id="cal-alice-main", event_id="evnone001")])
        ]
    }
    summary = await run(
        dataset, script, repeats=1, sut_factory=lambda: ScriptedToolSUT(catch_errors=False)
    )
    r = by_task(summary, "t-001")[0]
    assert (r.failure_type, r.detail) == ("connector_error_unhandled", NotFound.__name__)
    assert r.trajectory == ("get_event",)


async def test_catching_sut_feeds_error_back_and_continues(dataset: Dataset) -> None:
    script = good_script()
    script["t-001"].insert(
        0, step(calls=[call("get_event", calendar_id="cal-alice-main", event_id="evnone001")])
    )
    summary = await run(dataset, script, repeats=1)
    r = by_task(summary, "t-001")[0]
    assert r.success
    assert r.trajectory == ("get_event", "list_events")


async def test_result_unknown_write_attempt_is_forbidden_tool(dataset: Dataset) -> None:
    script = good_script()
    script["t-001"] = [
        step(
            calls=[
                call(
                    "create_event",
                    calendar_id="cal-alice-main",
                    event_id="evabc001",
                    draft={
                        "title": "신규",
                        "start": "2026-11-14T01:00:00+00:00",
                        "end": "2026-11-14T02:00:00+00:00",
                    },
                )
            ]
        ),
        step("주간 회의와 점심이 있습니다."),
    ]
    fault = FaultPlanSpec(
        "unknown-create",
        FaultPlan.from_dict({"rules": [{"tool": "create_event", "error": "result_unknown"}]}),
    )
    summary = await run(dataset, script, repeats=1, fault=fault)
    r = by_task(summary, "t-001")[0]
    assert r.failure_type == "forbidden_tool"
    assert r.trajectory == ("create_event",)
    assert r.config["fault_plan"] == "unknown-create"
    assert r.state_diff_match is False  # result_unknown은 쓰기를 적용한 뒤 오류를 낸다


async def test_forbidden_tool_wins_over_unhandled_connector_error(dataset: Dataset) -> None:
    fault = FaultPlanSpec(
        "fail-create",
        FaultPlan.from_dict({"rules": [{"tool": "create_event", "error": "transient"}]}),
    )
    script = {
        "t-001": [
            step(
                calls=[
                    call(
                        "create_event",
                        calendar_id="cal-alice-main",
                        event_id="evabc001",
                        draft={
                            "title": "신규",
                            "start": "2026-11-14T01:00:00+00:00",
                            "end": "2026-11-14T02:00:00+00:00",
                        },
                    )
                ]
            )
        ]
    }
    summary = await run(
        dataset,
        script,
        repeats=1,
        fault=fault,
        sut_factory=lambda: ScriptedToolSUT(catch_errors=False),
    )
    assert by_task(summary, "t-001")[0].failure_type == "forbidden_tool"


async def test_invalid_fault_plan_rule_is_rejected_before_running(dataset: Dataset) -> None:
    fault = FaultPlanSpec(
        "typo", FaultPlan.from_dict({"rules": [{"tool": "creat_event", "error": "transient"}]})
    )
    with pytest.raises(ValueError, match="creat_event"):
        await run(dataset, repeats=1, fault=fault)


async def test_each_episode_has_own_fixed_clock_and_forked_world(dataset: Dataset) -> None:
    seen: list[tuple[Any, Any, Any]] = []

    class Probe:
        name = "probe"
        version = "0"

        async def run(self, episode: Episode) -> EpisodeOutput:
            seen.append((episode.clock.now(), episode.task.as_of_time, episode.clock))
            created = await episode.connectors.calendar["acct-alice"].list_calendars()
            assert created
            return EpisodeOutput(final_text="안녕")

    await run(dataset, repeats=2, sut_factory=Probe)
    assert all(now == as_of for now, as_of, _ in seen)
    assert len({id(clock) for _, _, clock in seen}) == len(
        seen
    )  # 객체를 들고 있으므로 id가 겹치지 않는다


def test_episode_seed_is_stable_and_varies() -> None:
    assert episode_seed(1, "t-001", 1) == episode_seed(1, "t-001", 1)
    assert (
        len(
            {
                episode_seed(1, "t-001", 1),
                episode_seed(1, "t-001", 2),
                episode_seed(2, "t-001", 1),
                episode_seed(1, "t-002", 1),
            }
        )
        == 4
    )


# ---------------------------------------------------------------- 결정성·결과 스키마


def _normalized(summary: Any) -> list[dict[str, Any]]:
    rows = []
    for r in summary.results:
        row = r.to_dict()
        row.pop("latency_ms")  # 벽시계 측정값. 실행마다 다르다
        rows.append(row)
    return rows


async def test_same_config_gives_identical_results_except_latency(dataset: Dataset) -> None:
    first = await run(dataset, repeats=3, seed=7)
    second = await run(dataset, repeats=3, seed=7)
    assert _normalized(first) == _normalized(second)
    assert first.experiment_id == second.experiment_id
    assert first.config_fingerprint == second.config_fingerprint


async def test_fingerprint_changes_with_config(dataset: Dataset) -> None:
    a = await run(dataset, repeats=1, seed=1)
    b = await run(dataset, repeats=1, seed=2)
    c = await run(dataset, repeats=1, seed=1, model_id="other-model")
    assert len({a.config_fingerprint, b.config_fingerprint, c.config_fingerprint}) == 3


async def test_result_schema(dataset: Dataset) -> None:
    summary = await run(dataset, repeats=1, model_id="m-1", prompt_version="p-1")
    row = json.loads(summary.results[0].to_json_line())
    assert set(row) >= {
        "experiment_id", "config", "config_fingerprint", "task_id", "repeat", "outcome",
        "state_diff_match", "approval_violations", "trajectory", "usage", "latency_ms",
    }  # fmt: skip
    assert set(row["outcome"]) >= {"success", "failure_type"}
    assert set(row["usage"]) == {"llm_calls", "input_tokens", "output_tokens", "cost_usd"}
    assert row["approval_violations"] == 0
    cfg = row["config"]
    assert cfg["git_sha"] == "test-sha"
    assert cfg["dataset"] == "mini-eval@0.1.0" and cfg["split"] == "dev"
    assert cfg["sut"] == {"name": "scripted_tool", "version": "0"}
    assert cfg["model_id"] == "m-1" and cfg["prompt_version"] == "p-1"
    assert cfg["fault_plan"] == "none"
    assert cfg["budget"] == Budget().to_dict()
    assert SCRIPTED_MODEL_ID not in json.dumps(row)  # 설정의 model_id는 선언값이다


async def test_results_do_not_store_prompt_or_response_text(dataset: Dataset) -> None:
    summary = await run(dataset, repeats=2)
    blob = "\n".join(r.to_json_line() for r in summary.results)
    for t in dataset.tasks:
        for turn in t.turns:
            assert turn.content not in blob
    for marker in ("질문문장", "주간 회의와 점심이 있습니다", "팀 회고가 있습니다", "안녕하세요"):
        assert marker not in blob


# ---------------------------------------------------------------- 봉인·누적 상한·추정


async def test_test_split_is_sealed(dataset: Dataset) -> None:
    with pytest.raises(SealedSplitError):
        await run(dataset, split="test", repeats=1)
    summary = await run(dataset, split="test", repeats=1, allow_sealed_test=True, sut="noop")
    assert [r.task_id for r in summary.results] == ["t-004"]


async def test_cost_cap_stops_experiment(dataset: Dataset) -> None:
    # 에피소드당 0.002 USD. 두 번째 에피소드 뒤 0.004 > 0.0025
    summary = await run(dataset, repeats=3, experiment_budget=ExperimentBudget(max_cost_usd=0.0025))
    assert summary.stopped_reason == "cost_cap"
    assert len(summary.results) == 2
    assert summary.episodes_planned == 12


async def test_token_cap_replaces_cost_cap_when_cost_unknown(dataset: Dataset) -> None:
    script = good_script(cost=None)
    summary = await run(
        dataset,
        script,
        repeats=3,
        experiment_budget=ExperimentBudget(max_cost_usd=1.0, max_tokens=50),
    )
    # 에피소드당 30토큰: 두 번째 에피소드 뒤 60 > 50
    assert summary.stopped_reason == "token_cap"
    assert len(summary.results) == 2
    assert all(r.cost_usd is None for r in summary.results)


async def test_unknown_cost_without_token_cap_stops(dataset: Dataset) -> None:
    summary = await run(
        dataset,
        good_script(cost=None),
        repeats=2,
        experiment_budget=ExperimentBudget(max_cost_usd=1.0),
    )
    assert summary.stopped_reason == "cost_unknown_no_token_cap"
    assert len(summary.results) == 1


async def test_estimate_extrapolates_from_sample(dataset: Dataset) -> None:
    est, summary = await estimate_experiment(
        dataset,
        split="dev",
        n=2,
        repeats=3,
        seed=0,
        sut_factory=lambda: ScriptedToolSUT(),
        llm_factory=scripted_llm_factory(good_script()),
    )
    assert len(summary.results) == 2 and est.sampled_tasks == 2
    assert est.total_tasks == 4 and est.estimated_episodes == 12
    # t-005는 호출 1번(15토큰), 나머지는 2번(30토큰)이므로 표본 구성에 따라 달라진다
    per_episode = est.mean_input_tokens + est.mean_output_tokens
    assert est.estimated_total_tokens == pytest.approx(per_episode * 12)
    assert est.mean_cost_usd is not None
    assert est.estimated_total_cost_usd == pytest.approx(est.mean_cost_usd * 12)


async def test_sample_is_seeded(dataset: Dataset) -> None:
    a = await run(dataset, repeats=1, sample=2, seed=5)
    b = await run(dataset, repeats=1, sample=2, seed=5)
    assert [r.task_id for r in a.results] == [r.task_id for r in b.results]
    assert len(a.results) == 2


async def test_noop_sut_requires_no_llm(dataset: Dataset) -> None:
    summary = await run_experiment(
        dataset,
        split="dev",
        sut_factory=NoopSUT,
        llm_factory=scripted_llm_factory({}),
        repeats=1,
    )
    assert len(summary.results) == 4
