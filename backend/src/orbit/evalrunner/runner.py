"""에피소드 실행과 실험 제어 (ADR-0009 §3, D3-4, D10-3).

에피소드 절차: 월드 포크(시드 = sha256(실험 시드, task_id, repeat)) -> 에피소드 전용 고정
시계 -> FaultPlan 적용 -> `build_sim_connectors` -> SUT 실행(예산 감시) -> 상태 diff·호출 기록·
사용량 수집 -> 채점 -> 결과 한 줄. 궤적·상태·사용량은 SUT가 보고한 값이 아니라
Sim과 `UsageRecorder`에서 읽는다.

결과에는 프롬프트·응답·과제 질문 원문을 남기지 않는다(ADR-0006 취지). 디버그 옵션도 v0에는 없다.
"""

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from orbit.connectors.contracts import ConnectorError
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.world import SimWorld, SnapshotError, diff
from orbit.core.clock import FixedClock
from orbit.evalrunner.episode import (
    Budget,
    BudgetExceeded,
    Episode,
    EpisodeConnectors,
    EpisodeOutput,
    EpisodeRecordingLLM,
    ReadOnlyClock,
    StepCounter,
    SutTask,
    SutTurn,
    SystemUnderTest,
)
from orbit.evalrunner.scoring import FailureType, score_episode
from orbit.evalrunner.structured import answer_ids
from orbit.evalrunner.task_source import SPLITS, Dataset, DatasetError, TaskView
from orbit.llm.interface import LLM, UsageRecorder

UNSPECIFIED_MODEL = "unspecified"

LLMFactory = Callable[[TaskView, int], LLM]
SutFactory = Callable[[], SystemUnderTest]
ResultSink = Callable[["EpisodeResult"], None]


class SealedSplitError(RuntimeError):
    """봉인된 test 분할을 명시 확인 없이 실행하려 했다 (ADR-0009 §2, D2-3)."""


def episode_seed(experiment_seed: int, task_id: str, repeat: int) -> int:
    """(실험 시드, 과제, 반복 번호)에서 결정적으로 정하는 월드 포크 시드. `hash()`는 쓰지 않는다."""
    raw = f"{experiment_seed}\x00{task_id}\x00{repeat}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def _digest(data: Any) -> str:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExperimentBudget:
    """실험 단위 누적 상한. 에피소드 경계에서 검사하므로 마지막 에피소드만큼 넘을 수 있다.

    `max_cost_usd`를 쓰려면 모든 호출의 비용을 알아야 한다. 비용을 모르는 호출이 있으면
    `max_tokens`가 대신 상한이 되고, `max_tokens`가 없으면 상한을 강제할 수 없으므로 중단한다.
    """

    max_cost_usd: float | None = None
    max_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"max_cost_usd": self.max_cost_usd, "max_tokens": self.max_tokens}


@dataclass(frozen=True)
class FaultPlanSpec:
    name: str = "none"
    plan: FaultPlan | None = None

    @property
    def digest(self) -> str:
        content = self.plan.model_dump(mode="json") if self.plan is not None else None
        return _digest(content)


def _config_dict(
    *,
    dataset: Dataset,
    split: str,
    sut: SystemUnderTest,
    model_id: str,
    prompt_version: str,
    budget: Budget,
    experiment_budget: ExperimentBudget,
    fault: FaultPlanSpec,
    git_sha: str,
    git_dirty: bool,
    seed: int,
    repeats: int,
    sample: int | None,
    n_tasks: int,
) -> dict[str, Any]:
    return {
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "dataset": f"{dataset.name}@{dataset.version}",
        "split": split,
        "sut": {"name": sut.name, "version": sut.version},
        "model_id": model_id,
        "prompt_version": prompt_version,
        "budget": budget.to_dict(),
        "experiment_budget": experiment_budget.to_dict(),
        "fault_plan": fault.name,
        "fault_plan_digest": fault.digest,
        "experiment_seed": seed,
        "repeats": repeats,
        "sample": sample,
        "n_tasks": n_tasks,
    }


@dataclass(frozen=True)
class EpisodeResult:
    experiment_id: str
    config: Mapping[str, Any]
    config_fingerprint: str
    task_id: str
    scenario_id: str
    cluster_id: str  # 근거를 공유하는 과제 묶음(통계의 재표본 단위)
    repeat: int
    success: bool | None  # None: 채점하지 못한 항목이 있어 성공도 실패도 아니다
    failure_type: FailureType | None
    detail: str | None
    state_diff_match: bool | None
    checks: Mapping[str, bool | None]
    unscored: tuple[str, ...]
    text_fact_recall: float | None  # 구조화 채점 과제에서만. 사실이 없으면 None
    answer_ids: dict[str, Any] | None  # SUT가 낸 구조화된 답의 일정 ID·시각 요약(원문 없음)
    trajectory: tuple[str, ...]
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    models: tuple[str, ...]
    providers: tuple[str, ...]
    model_mismatch: bool
    latency_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "config": dict(self.config),
            "config_fingerprint": self.config_fingerprint,
            "task_id": self.task_id,
            "scenario_id": self.scenario_id,
            "cluster_id": self.cluster_id,
            "repeat": self.repeat,
            "outcome": {
                "success": self.success,
                "failure_type": self.failure_type,
                "detail": self.detail,
            },
            "state_diff_match": self.state_diff_match,
            "approval_violations": 0,  # v0 고정. 승인 경로는 4단계에서 채운다
            "checks": dict(self.checks),
            "unscored": list(self.unscored),
            "text_fact_recall": self.text_fact_recall,
            "answer_ids": self.answer_ids,
            "trajectory": list(self.trajectory),
            "usage": {
                "llm_calls": self.llm_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost_usd": self.cost_usd,
                "models": list(self.models),
                "providers": list(self.providers),
            },
            "model_mismatch": self.model_mismatch,
            "latency_ms": self.latency_ms,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


async def run_episode(
    *,
    task: TaskView,
    base_world: SimWorld,
    sut: SystemUnderTest,
    llm: LLM,
    budget: Budget,
    fault: FaultPlanSpec,
    seed: int,
    repeat: int,
    experiment_id: str,
    config: Mapping[str, Any],
    fingerprint: str,
    declared_model_id: str = UNSPECIFIED_MODEL,
    monotonic: Callable[[], float] = time.perf_counter,
) -> EpisodeResult:
    clock = FixedClock(task.as_of_time)  # 에피소드마다 자기 시계(D1-5)
    world = base_world.fork(episode_seed(seed, task.task_id, repeat), clock=clock)
    before = world.copy()
    connectors = build_sim_connectors(world, [task.account_id], fault.plan)
    recorder = UsageRecorder()
    recording_llm = EpisodeRecordingLLM(llm, recorder)
    account = task.account_id
    step_counter = StepCounter(budget, recorder, connectors.calls, monotonic=monotonic)
    episode = Episode(
        # 정답(gold)과 분할·태그는 러너만 가진다. SUT에는 아래 네 필드만 준다.
        task=SutTask(
            task_id=task.task_id,
            turns=tuple(SutTurn(t.role, t.content) for t in task.turns),
            as_of_time=task.as_of_time,
            account_id=account,
        ),
        connectors=EpisodeConnectors(
            mail=connectors.mail[account], calendar=connectors.calendar[account]
        ),
        clock=ReadOnlyClock(clock),
        llm=recording_llm,
        budget=budget,
        steps=step_counter,
    )

    error_type: FailureType | None = None
    detail: str | None = None
    output: EpisodeOutput | None = None
    started = monotonic()
    timeout = asyncio.timeout(budget.max_seconds)
    try:
        async with timeout:
            result = await sut.run(episode)
        if isinstance(result, EpisodeOutput) and isinstance(result.final_text, str):
            output = result
        else:
            error_type, detail = "sut_error", "InvalidOutput"
    except BudgetExceeded as exc:
        error_type, detail = "budget_exceeded", exc.reason
    except TimeoutError as exc:
        if timeout.expired():
            error_type, detail = "budget_exceeded", "time"
        else:
            error_type, detail = "sut_error", type(exc).__name__
    except ConnectorError as exc:
        error_type, detail = "connector_error_unhandled", type(exc).__name__
    except Exception as exc:  # SUT의 어떤 오류도 에피소드 실패로 기록한다
        error_type, detail = "sut_error", type(exc).__name__
    latency_ms = round((monotonic() - started) * 1000)

    calls = connectors.calls.records
    usage = recorder.total_usage
    if error_type is None:  # SUT가 `consume()`을 건너뛰어도 관찰한 호출 수로 다시 검사한다
        over = step_counter.verify()
        if over is not None:
            error_type, detail = "budget_exceeded", over

    score = score_episode(
        task.gold,
        output.final_text if output is not None else "",
        calls,
        diff(before, world),
        output.structured if output is not None else None,
    )
    failure: FailureType | None
    if score.failure_type in ("forbidden_tool", "unexpected_state_change"):
        # 안전 위반은 오류·예산 초과에 가려지지 않는다
        failure, detail = score.failure_type, None
    elif error_type is not None:
        failure = error_type
    else:
        failure = score.failure_type
    recorded = recorder.total_cost_usd
    # 취소된 호출의 비용은 알 수 없다. 합계가 있어도 과소 집계이므로 None으로 둔다.
    cost = None if recording_llm.cancelled or recorded is None else float(recorded)
    models = tuple(sorted({r.model_id for r in recorder.records if r.model_id}))
    providers = tuple(sorted({r.provider for r in recorder.records if r.provider}))
    mismatch = (
        declared_model_id != UNSPECIFIED_MODEL
        and bool(models)
        and set(models) != {declared_model_id}
    )
    checks: Mapping[str, bool | None] = score.checks
    if error_type is not None:  # 응답이 없으면 응답·호출 검사는 의미가 없다
        checks = {
            k: (v if k in ("forbidden_tools", "state_diff") else None)
            for k, v in score.checks.items()
        }
    return EpisodeResult(
        experiment_id=experiment_id,
        config=config,
        config_fingerprint=fingerprint,
        task_id=task.task_id,
        scenario_id=task.scenario_id,
        cluster_id=task.cluster_id,
        repeat=repeat,
        success=None if failure == "unscored" else failure is None,
        failure_type=failure,
        detail=detail if failure == error_type else None,
        state_diff_match=score.state_diff_match,
        checks=checks,
        unscored=score.unscored,
        text_fact_recall=None if error_type is not None else score.text_fact_recall,
        answer_ids=answer_ids(output.structured) if output is not None else None,
        trajectory=tuple(c.tool for c in calls),
        llm_calls=len(recorder.records),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        models=models,
        providers=providers,
        model_mismatch=mismatch,
        latency_ms=latency_ms,
    )


@dataclass
class ExperimentSummary:
    experiment_id: str
    config_fingerprint: str
    episodes_planned: int
    results: list[EpisodeResult] = field(default_factory=list)
    stopped_reason: str | None = None  # None이면 끝까지 실행했다
    total_cost_usd: float = 0.0  # 비용을 아는 에피소드의 합
    cost_unknown_episodes: int = 0
    total_tokens: int = 0

    @property
    def completed(self) -> bool:
        return self.stopped_reason is None


def select_tasks(tasks: Sequence[TaskView], sample: int | None, seed: int) -> tuple[TaskView, ...]:
    """`sample`이 있으면 task_id 정렬 목록에서 시드 고정으로 뽑는다(결과는 task_id 순)."""
    if sample is None:
        return tuple(tasks)
    if sample < 1:
        raise ValueError("sample은 1 이상이어야 한다")
    ordered = sorted(tasks, key=lambda t: t.task_id)
    rng = random.Random(seed)  # noqa: S311  재현성용 의사난수
    picked = rng.sample(ordered, min(sample, len(ordered)))
    return tuple(sorted(picked, key=lambda t: t.task_id))


async def run_experiment(
    dataset: Dataset,
    *,
    split: str,
    sut_factory: SutFactory,
    llm_factory: LLMFactory,
    repeats: int = 3,
    seed: int = 0,
    sample: int | None = None,
    budget: Budget | None = None,
    experiment_budget: ExperimentBudget | None = None,
    fault: FaultPlanSpec | None = None,
    model_id: str = "unspecified",
    prompt_version: str = "unspecified",
    git_sha: str = "unknown",
    git_dirty: bool = True,  # git 상태를 모르면 깨끗하다고 가정하지 않는다
    experiment_id: str | None = None,
    allow_sealed_test: bool = False,
    sink: ResultSink | None = None,
    monotonic: Callable[[], float] = time.perf_counter,
) -> ExperimentSummary:
    """반복 우선(1회차 전 과제 -> 2회차 ...)으로 실행한다. 과제 순서는 실험 시드로 고정 셔플해
    순서 편향(누적 상한 중단 시 특정 과제만 남는 문제)을 줄인다. 결과는 끝날 때마다 `sink`로 넘기고
    요약에도 모은다.

    SUT는 에피소드마다 `sut_factory()`로 새로 만들고, LLM은 `llm_factory(task, repeat)`로
    새로 받는다(에피소드 간 상태 공유 없음).
    """
    if split not in SPLITS:
        raise ValueError(f"split은 {SPLITS} 중 하나여야 한다: {split!r}")
    if split == "test" and not allow_sealed_test:
        raise SealedSplitError("test 분할은 봉인돼 있다. 명시 확인 없이는 실행하지 않는다")
    if repeats < 1:
        raise ValueError("repeats는 1 이상이어야 한다")
    budget = budget or Budget()
    experiment_budget = experiment_budget or ExperimentBudget()
    fault = fault or FaultPlanSpec()

    tasks = select_tasks(dataset.tasks_for(split), sample, seed)
    if not tasks:
        raise ValueError(f"{split} 분할에 과제가 없다")
    try:
        base_world = SimWorld.load(dict(dataset.world_snapshot), FixedClock(tasks[0].as_of_time))
    except SnapshotError as exc:  # pydantic 오류 메시지에는 입력값이 들어 있을 수 있다
        raise DatasetError("world.json이 Sim 스냅샷 v1 형식과 맞지 않는다") from exc
    for account in {t.account_id for t in tasks}:  # FaultPlan 오타를 실행 전에 드러낸다
        build_sim_connectors(base_world.fork(0), [account], fault.plan)

    # LLM 인스턴스를 따로 만들지 않고 팩토리 메타데이터(`is_scripted`)로 판별한다.
    if model_id == UNSPECIFIED_MODEL and not getattr(llm_factory, "is_scripted", False):
        raise ValueError("ScriptedLLM이 아닌 LLM에는 --model-id(고정 모델 ID)를 지정해야 한다")
    probe = sut_factory()
    config = _config_dict(
        dataset=dataset,
        split=split,
        sut=probe,
        model_id=model_id,
        prompt_version=prompt_version,
        budget=budget,
        experiment_budget=experiment_budget,
        fault=fault,
        git_sha=git_sha,
        git_dirty=git_dirty,
        seed=seed,
        repeats=repeats,
        sample=sample,
        n_tasks=len(tasks),
    )
    fingerprint = _digest(config)
    exp_id = experiment_id or f"exp-{fingerprint[:12]}"
    summary = ExperimentSummary(exp_id, fingerprint, len(tasks) * repeats)

    ordered = sorted(tasks, key=lambda t: t.task_id)
    random.Random(f"order:{seed}").shuffle(ordered)  # noqa: S311
    for repeat in range(1, repeats + 1):
        for task in ordered:
            result = await run_episode(
                task=task,
                base_world=base_world,
                sut=sut_factory(),
                llm=llm_factory(task, repeat),
                budget=budget,
                fault=fault,
                seed=seed,
                repeat=repeat,
                experiment_id=exp_id,
                config=config,
                fingerprint=fingerprint,
                declared_model_id=model_id,
                monotonic=monotonic,
            )
            summary.results.append(result)
            if sink is not None:
                sink(result)
            summary.total_tokens += result.input_tokens + result.output_tokens
            if result.cost_usd is None:
                summary.cost_unknown_episodes += 1
            else:
                summary.total_cost_usd += result.cost_usd
            reason = _stop_reason(summary, experiment_budget)
            if reason is not None:
                summary.stopped_reason = reason
                return summary
    return summary


def _stop_reason(summary: ExperimentSummary, cap: ExperimentBudget) -> str | None:
    if cap.max_tokens is not None and summary.total_tokens > cap.max_tokens:
        return "token_cap"
    if cap.max_cost_usd is not None:
        if summary.cost_unknown_episodes:
            if cap.max_tokens is None:
                return "cost_unknown_no_token_cap"
        elif summary.total_cost_usd > cap.max_cost_usd:
            return "cost_cap"
    return None


@dataclass(frozen=True)
class Estimate:
    sampled_tasks: int
    episodes_run: int
    total_tasks: int
    repeats: int
    mean_input_tokens: float
    mean_output_tokens: float
    mean_cost_usd: float | None  # 비용을 모르는 에피소드가 있으면 None
    estimated_episodes: int
    estimated_total_tokens: float
    estimated_total_cost_usd: float | None
    stopped_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


async def estimate_experiment(
    dataset: Dataset, *, split: str, n: int, repeats: int = 3, seed: int = 0, **kwargs: Any
) -> tuple[Estimate, ExperimentSummary]:
    """과제 n개를 1회씩 실행해 전체(분할 전체 x repeats) 토큰·비용을 단순 평균으로 추정한다.

    추정은 표본 평균의 외삽일 뿐이고 과제별 편차는 반영하지 않는다. 호출자는 실제 실험과 다른
    파일에 결과를 쓴다.
    """
    summary = await run_experiment(dataset, split=split, repeats=1, seed=seed, sample=n, **kwargs)
    results = summary.results
    total_tasks = len(dataset.tasks_for(split))
    count = max(len(results), 1)
    mean_in = sum(r.input_tokens for r in results) / count
    mean_out = sum(r.output_tokens for r in results) / count
    mean_cost = summary.total_cost_usd / count if summary.cost_unknown_episodes == 0 else None
    episodes = total_tasks * repeats
    return (
        Estimate(
            sampled_tasks=len({r.task_id for r in results}),
            episodes_run=len(results),
            total_tasks=total_tasks,
            repeats=repeats,
            mean_input_tokens=mean_in,
            mean_output_tokens=mean_out,
            mean_cost_usd=mean_cost,
            estimated_episodes=episodes,
            estimated_total_tokens=(mean_in + mean_out) * episodes,
            estimated_total_cost_usd=mean_cost * episodes if mean_cost is not None else None,
            stopped_reason=summary.stopped_reason,
        ),
        summary,
    )
