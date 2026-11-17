"""에피소드 실행과 실험 제어 (ADR-0009 §3, D3-4, D10-3).

에피소드 절차: 월드 포크(시드 = sha256(실험 시드, task_id, repeat)) -> 에피소드 전용 고정
시계 -> FaultPlan 적용 -> `build_sim_connectors` -> SUT 실행(예산 감시) -> 상태 diff·호출 기록·
사용량 수집 -> 채점 -> 결과 한 줄. 궤적·상태·사용량은 SUT가 보고한 값이 아니라
Sim과 `UsageRecorder`에서 읽는다.

결과에는 지표를 나중에 더할 수 있도록 턴별 발화·답 문장·도구 호출 결과 본문을 남긴다(ADR-0029 §1,
`trace`). 평가 데이터는 합성이고 결과 파일(`runs/`)은 git에서 제외한다. 실사용 DB·그래프 상태 규칙
(ADR-0006)과는 무관하다. 결과 스키마 버전은 `result_schema`다(1은 이 항목이 없던 v0 결과).
"""

import asyncio
import hashlib
import json
import logging
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
from orbit.evaldata.schema import DEFAULT_TIME_ZONE
from orbit.evalrunner.episode import (
    Budget,
    BudgetExceeded,
    Episode,
    EpisodeConnectors,
    EpisodeDatabase,
    EpisodeOutput,
    EpisodeRecordingLLM,
    ReadOnlyClock,
    StepCounter,
    SutTask,
    SutTurn,
    SystemUnderTest,
)
from orbit.evalrunner.metrics_v1 import task_meta
from orbit.evalrunner.oracle import min_tool_calls
from orbit.evalrunner.retrieval.embed_cache import BudgetExceeded as EmbedBudgetExceeded
from orbit.evalrunner.runflow_env import (
    DbObservation,
    RunflowEnvironment,
    approval_violations,
)
from orbit.evalrunner.scoring import FailureType, score_episode
from orbit.evalrunner.structured import answer_ids
from orbit.evalrunner.task_source import SPLITS, Dataset, DatasetError, TaskView
from orbit.evalrunner.trace import RESULT_SCHEMA_VERSION, EpisodeTrace, build_trace
from orbit.llm.interface import LLM, UsageRecorder
from orbit.llm.model_id import is_declared_model

logger = logging.getLogger(__name__)

UNSPECIFIED_MODEL = "unspecified"
# 평가 자원 정리는 에피소드 예산과 별도로 이 유예 시간 안에서 시도한다(ADR-0034).
DATABASE_CLEANUP_SECONDS = 5.0

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
    llm_config: Mapping[str, Any] | None = None,
    smoke_excluded: int | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
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
    if smoke_excluded is not None:  # 스모크 목록에서 검수 제외로 빠진 과제 수(ADR-0026 §5)
        config["smoke_excluded"] = smoke_excluded
    if llm_config is not None:  # 실제 LLM 제공자 설정(ADR-0014). 스크립트 실행에는 없다.
        config["llm"] = dict(llm_config)
    sut_info = getattr(sut, "config_info", None)
    if callable(
        sut_info
    ):  # runflow 지문·답 제출 확장·색인 모드처럼 평가 대상이 더하는 설정(ADR-0023)
        config["sut_config"] = sut_info(
            model_id=model_id, llm_info=dict(llm_config) if llm_config is not None else None
        )
    return config


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
    # 커넥터 호출 기록(도구, 요약된 인자, 결과, 오류 종류). 인자는 `CallRecord.args` 요약이라
    # 본문 필드와 80자 넘는 문자열은 길이 표시다(ADR-0026 §7). 결과 요약은 넣지 않는다.
    calls: tuple[Mapping[str, Any], ...] = ()
    # 정답 종류(`expected_answer.kind`). 없으면 None. 통계가 정답이 clarify인 문항을 가른다.
    expected_kind: str | None = None
    # `ambiguity_ignored`일 때만: 제출이 일치한 해석 번호(readings 인덱스, 0부터)
    matched_reading: int | None = None
    text_skipped: bool = False  # 최종 텍스트를 받지 못해 텍스트 지표가 없다
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0  # output_tokens의 부분집합(추론에 쓴 토큰)
    length_finishes: int = 0  # 출력 토큰 한도로 끝난 LLM 응답 수(finish_reason == "length")
    # 아래 셋은 DB를 쓰는 평가 대상(`runflow_v0`)에서만 값이 있다. 없으면 결과 행에도 없다.
    approval_violations: int = 0
    turns: tuple[Mapping[str, Any], ...] | None = None  # 턴별 run 상태·승인 수(러너가 DB에서 읽음)
    reminders_created: int | None = None
    cleanup_failed: bool | None = None  # 평가 DB 정리 실패 여부(runflow_v0에서만)
    time_zone: str = DEFAULT_TIME_ZONE  # 이 에피소드에서 평가 대상이 쓴 사용자 시간대(과제 속성)
    # 과제 전용 장애 주입의 규칙 요약("도구:오류"). 없으면 None. 평가 대상에게는 안 보인다.
    task_fault_rules: tuple[str, ...] | None = None
    # 기대 변화별(기대 순서) 일치 여부. `expected_changes`가 비어 있지 않은 과제에서만 값이 있다
    # (값 원문 대신 일치 여부만 남긴다).
    change_matches: tuple[bool, ...] | None = None
    # 아래는 ADR-0029에서 더한 기록이다. 채점에는 쓰지 않는다.
    change_fields: tuple[Mapping[str, Any], ...] | None = None  # 기대 변화별 필드 일치(보조 지표)
    trace: Mapping[str, Any] | None = None  # 턴별 발화·답 문장·도구 호출 결과·핸들 대응표
    task_meta: Mapping[str, Any] | None = None  # 과제 유형·시나리오·근거 태그 ID(정답 아님)
    min_tool_calls: int | None = None  # 정답 제출 오라클의 도구 호출 수(하한)
    result_schema: int = RESULT_SCHEMA_VERSION
    # 단계 종류별 LLM 호출 수(평가 전용 Agent 패턴, ADR-0030). 종류가 붙은 호출이 없으면 None.
    llm_calls_by_kind: Mapping[str, int] | None = None
    # 패턴의 계획·분류 합계(분류 파싱 실패 수 등). 패턴이 없으면 None. 오류로 끝난 에피소드는 None.
    pattern_stats: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        row = self._base_dict()
        if self.llm_calls_by_kind is not None:
            row["usage"]["llm_calls_by_kind"] = dict(sorted(self.llm_calls_by_kind.items()))
        if self.pattern_stats is not None:
            row["pattern_stats"] = dict(self.pattern_stats)
        if self.cleanup_failed is not None:  # DB를 쓴 에피소드(관찰이 실패하면 turns가 없다)
            row["approval_violations"] = self.approval_violations
            row["turns"] = None if self.turns is None else [dict(t) for t in self.turns]
            row["reminders_created"] = self.reminders_created
            row["cleanup_failed"] = bool(self.cleanup_failed)
        return row

    def _base_dict(self) -> dict[str, Any]:
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
            "approval_violations": 0,  # 승인 경로가 없는 평가 대상은 0
            "checks": dict(self.checks),
            "unscored": list(self.unscored),
            "text_fact_recall": self.text_fact_recall,
            "answer_ids": self.answer_ids,
            "expected_kind": self.expected_kind,
            "time_zone": self.time_zone,
            "task_fault": None if self.task_fault_rules is None else list(self.task_fault_rules),
            "change_matches": None if self.change_matches is None else list(self.change_matches),
            "change_fields": (
                None if self.change_fields is None else [dict(c) for c in self.change_fields]
            ),
            "result_schema": self.result_schema,
            "task_meta": None if self.task_meta is None else dict(self.task_meta),
            "min_tool_calls": self.min_tool_calls,
            "trace": None if self.trace is None else dict(self.trace),
            "matched_reading": self.matched_reading,
            "trajectory": list(self.trajectory),
            "calls": [dict(c) for c in self.calls],
            "usage": {
                "llm_calls": self.llm_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "cost_usd": self.cost_usd,
                "models": list(self.models),
                "providers": list(self.providers),
                "length_finishes": self.length_finishes,
            },
            "model_mismatch": self.model_mismatch,
            "latency_ms": self.latency_ms,
            "text_skipped": self.text_skipped,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def task_fault_plan(task: TaskView) -> FaultPlan | None:
    """과제 전용 장애 주입. 없으면 None. 형식 오류는 데이터셋 로드가 먼저 거부한다."""
    return FaultPlan.from_dict(task.fault_plan) if task.fault_plan is not None else None


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
    database_env: RunflowEnvironment | None = None,
    min_calls: int | None = None,
) -> EpisodeResult:
    clock = FixedClock(task.as_of_time)  # 에피소드마다 자기 시계(D1-5)
    world = base_world.fork(episode_seed(seed, task.task_id, repeat), clock=clock)
    before = world.copy()
    task_plan = task_fault_plan(task)
    if task_plan is not None and fault.plan is not None:
        raise ValueError("과제 fault_plan과 실행 fault_plan(--fault-plan)을 함께 쓸 수 없다")
    connectors = build_sim_connectors(
        world, [task.account_id], task_plan if task_plan is not None else fault.plan
    )
    time_zone = task.time_zone or DEFAULT_TIME_ZONE
    recorder = UsageRecorder()
    recording_llm = EpisodeRecordingLLM(llm, recorder)
    account = task.account_id
    step_counter = StepCounter(budget, recorder, connectors.calls, monotonic=monotonic)
    collector = EpisodeTrace()
    # 평가 DB 워크스페이스는 러너가 준비하고 정리한다. 평가 대상은 이 핸들만 쓴다(ADR-0023 §2).
    database: EpisodeDatabase | None = None
    error_type: FailureType | None = None
    detail: str | None = None
    output: EpisodeOutput | None = None
    observation: DbObservation | None = None
    cleanup_failed = False
    started = monotonic()
    latency_ms: int | None = None
    timeout = asyncio.timeout(budget.max_seconds)
    try:
        # DB 준비·SUT·관찰이 같은 절대 기한을 공유한다. 단계마다 예산을 다시 시작하지 않는다.
        async with timeout:
            if database_env is not None:
                database = await database_env.prepare(
                    as_of=task.as_of_time, seed=episode_seed(seed, task.task_id, repeat)
                )
            # 구성 중 예외가 나도 finally가 자원을 정리한다.
            episode = Episode(
                task=SutTask(
                    task_id=task.task_id,
                    turns=tuple(SutTurn(t.role, t.content) for t in task.turns),
                    as_of_time=task.as_of_time,
                    account_id=account,
                    approval_policy=tuple(task.approval_policy or ()),
                    time_zone=time_zone,
                ),
                connectors=EpisodeConnectors(
                    mail=connectors.mail[account],
                    calendar=connectors.calendar[account],
                    drive=connectors.drive[account],
                ),
                clock=ReadOnlyClock(clock),
                llm=recording_llm,
                budget=budget,
                steps=step_counter,
                database=database,
                trace=collector,
            )
            started = monotonic()  # 정상 결과의 지연 지표는 기존처럼 SUT 실행만 잰다.
            try:
                result = await sut.run(episode)
                if isinstance(result, EpisodeOutput) and isinstance(result.final_text, str):
                    output = result
                else:
                    error_type, detail = "sut_error", "InvalidOutput"
            except BudgetExceeded as exc:
                error_type, detail = "budget_exceeded", exc.reason
            except TimeoutError as exc:
                # 에피소드 기한의 CancelledError는 바깥에서 TimeoutError로 바뀐다.
                error_type, detail = "sut_error", type(exc).__name__
            except EmbedBudgetExceeded:
                raise  # 에피소드 실패가 아니라 실험 중단이다(`run_experiment`가 받는다)
            except ConnectorError as exc:
                error_type, detail = "connector_error_unhandled", type(exc).__name__
            except Exception as exc:
                error_type, detail = "sut_error", type(exc).__name__
            latency_ms = round((monotonic() - started) * 1000)
            if database_env is not None and database is not None:
                try:
                    observation = await database_env.observe(database)
                except Exception as exc:
                    error_type, detail = "harness_error", type(exc).__name__
    except TimeoutError:
        if not timeout.expired():
            raise  # 준비 자체의 TimeoutError는 예산 초과와 구분한다.
        error_type, detail = "budget_exceeded", "time"
        if database_env is not None and database is None:
            # prepare가 반환하지 않으면 부분적으로 만든 자원의 상태를 보장할 수 없다.
            cleanup_failed = True
    finally:
        if latency_ms is None:
            latency_ms = round((monotonic() - started) * 1000)
        if database_env is not None and database is not None:
            try:
                async with asyncio.timeout(DATABASE_CLEANUP_SECONDS):
                    await database_env.cleanup(database)
            except Exception as exc:
                # 남은 행은 같은 ID의 다음 `prepare`가 치운다. ID와 예외 이름만 남긴다.
                cleanup_failed = True
                logger.warning(
                    "평가 DB 정리 실패: workspace_id=%s error=%s",
                    database.workspace_id,
                    type(exc).__name__,
                )

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
        reminders_created=len(observation.reminders) if observation is not None else 0,
        reminders=observation.reminders if observation is not None else (),
    )
    violations = approval_violations(observation, calls) if observation is not None else 0
    failure: FailureType | None
    if score.failure_type == "forbidden_tool":
        # 안전 위반은 오류·예산 초과에 가려지지 않는다
        failure, detail = score.failure_type, None
    elif error_type == "harness_error":
        # 러너가 DB를 관찰하지 못해 승인 위반·리마인더를 알 수 없다. 평가 대상의 실패와 구분한다.
        # 다만 Sim diff만으로 아는 `unexpected_state_change`는 가리지 않는다(리마인더 제외).
        if score.failure_type == "unexpected_state_change":
            failure, detail = score.failure_type, None
        else:
            failure = "harness_error"
    elif violations > 0:  # 승인 없는 외부 쓰기는 `forbidden_tool`과 같은 안전 단계다(ADR-0023 §4)
        failure, detail = "approval_violation", None
    elif score.failure_type == "unexpected_state_change":
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
        and any(not is_declared_model(declared_model_id, m) for m in models)
    )
    checks: Mapping[str, bool | None] = score.checks
    if error_type is not None:  # 응답이 없으면 응답·호출 검사는 의미가 없다
        checks = {
            k: (v if k in ("forbidden_tools", "state_diff", "state_changes") else None)
            for k, v in score.checks.items()
        }
    if observation is not None:
        checks = {**checks, "approval": violations == 0}
    text_skipped = output is not None and output.text_skipped
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
        text_fact_recall=None if error_type is not None or text_skipped else score.text_fact_recall,
        answer_ids=answer_ids(output.structured) if output is not None else None,
        trajectory=tuple(c.tool for c in calls),
        calls=tuple(
            {
                "tool": c.tool,
                "args": c.args,
                "outcome": c.outcome,
                "error_type": c.error_type,
                "fault": c.fault,  # 주입된 장애 규칙("도구:오류"). 없으면 None
                "latency_ms": c.latency_ms,  # 커넥터 호출 시간(Sim 내부)
            }
            for c in calls
        ),
        expected_kind=(
            task.gold.expected_answer.kind if task.gold.expected_answer is not None else None
        ),
        matched_reading=score.matched_reading if failure == "ambiguity_ignored" else None,
        llm_calls=len(recorder.records),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cost_usd=cost,
        models=models,
        providers=providers,
        model_mismatch=mismatch,
        latency_ms=latency_ms,
        text_skipped=text_skipped,
        length_finishes=sum(r.finish_reason == "length" for r in recorder.records),
        approval_violations=violations,
        turns=(tuple(t.to_dict() for t in observation.turns) if observation is not None else None),
        reminders_created=len(observation.reminders) if observation is not None else None,
        cleanup_failed=cleanup_failed if database_env is not None else None,
        time_zone=time_zone,
        task_fault_rules=(
            tuple(f"{r.tool}:{r.error}" for r in task_plan.rules) if task_plan is not None else None
        ),
        change_matches=score.change_matches,
        change_fields=score.change_fields,
        trace=build_trace(
            user_turns=[t.content for t in task.turns if t.role == "user"],
            final_text=output.final_text if output is not None else None,
            structured=output.structured if output is not None else None,
            collector=collector,
            transcript=(
                observation.transcript
                if observation is not None and observation.transcript
                else None
            ),
        ),
        task_meta=task_meta(task),
        min_tool_calls=min_calls,
        llm_calls_by_kind=_calls_by_kind(recorder),
        pattern_stats=output.pattern_stats if output is not None else None,
    )


def _calls_by_kind(recorder: UsageRecorder) -> dict[str, int] | None:
    kinds = [r.step_kind for r in recorder.records if r.step_kind]
    return {k: kinds.count(k) for k in sorted(set(kinds))} if kinds else None


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
    llm_config: Mapping[str, Any] | None = None,
    smoke_excluded: int | None = None,
    sink: ResultSink | None = None,
    monotonic: Callable[[], float] = time.perf_counter,
    database_env: RunflowEnvironment | None = None,
) -> ExperimentSummary:
    """반복 우선(1회차 전 과제 -> 2회차 ...)으로 실행한다. 과제 순서는 실험 시드로 고정 셔플해
    순서 편향(누적 상한 중단 시 특정 과제만 남는 문제)을 줄인다. 결과는 끝날 때마다 `sink`로 넘기고
    요약에도 모은다.

    SUT는 에피소드마다 `sut_factory()`로 새로 만들고, LLM은 `llm_factory(task, repeat)`로
    새로 받는다(에피소드 간 상태 공유 없음). `database_env`가 있으면 에피소드마다 평가 DB
    워크스페이스를 만들고 끝난 뒤 지우며, 실험이 끝나면(중단해도) DB 접속을 닫는다(ADR-0023 §2).
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
    if fault.plan is not None and any(t.fault_plan is not None for t in tasks):
        # 두 장애 주입이 섞이면 결과 해석이 어렵다. 실행 전에 거부한다.
        raise ValueError("과제 fault_plan이 있는 데이터셋에는 --fault-plan을 함께 쓸 수 없다")
    for account in {t.account_id for t in tasks}:  # FaultPlan 오타를 실행 전에 드러낸다
        build_sim_connectors(base_world.fork(0), [account], fault.plan)
    for t in tasks:
        plan = task_fault_plan(t)
        if plan is not None:
            build_sim_connectors(base_world.fork(0), [t.account_id], plan)

    floors = await _min_tool_calls(tasks, base_world, dataset.world_snapshot)

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
        llm_config=llm_config,
        smoke_excluded=smoke_excluded,
    )
    fingerprint = _digest(config)
    exp_id = experiment_id or f"exp-{fingerprint[:12]}"
    summary = ExperimentSummary(exp_id, fingerprint, len(tasks) * repeats)

    try:
        ordered = sorted(tasks, key=lambda t: t.task_id)
        random.Random(f"order:{seed}").shuffle(ordered)  # noqa: S311
        for repeat in range(1, repeats + 1):
            for task in ordered:
                try:
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
                        database_env=database_env,
                        min_calls=floors.get(task.task_id),
                    )
                except EmbedBudgetExceeded:
                    # 질의 임베딩 비용 상한. 이 에피소드는 결과에 넣지 않는다.
                    summary.stopped_reason = "embedding_cost"
                    return summary
                summary.results.append(result)
                if sink is not None:
                    sink(result)
                summary.total_tokens += result.input_tokens + result.output_tokens
                if result.cost_usd is None:
                    summary.cost_unknown_episodes += 1
                else:
                    summary.total_cost_usd += result.cost_usd
                if result.cleanup_failed:
                    # 정리가 끝나지 않은 워크스페이스·연결을 둔 채 다음 과제를 시작하지 않는다.
                    summary.stopped_reason = "database_cleanup_failed"
                    return summary
                reason = _stop_reason(summary, experiment_budget)
                if reason is not None:
                    summary.stopped_reason = reason
                    return summary
        return summary
    finally:
        if database_env is not None:
            try:
                async with asyncio.timeout(DATABASE_CLEANUP_SECONDS):
                    await database_env.aclose()
            except Exception as exc:
                summary.stopped_reason = "database_cleanup_failed"
                logger.warning("평가 DB 종료 실패: error=%s", type(exc).__name__)


async def _min_tool_calls(
    tasks: Sequence[TaskView], base_world: SimWorld, snapshot: Mapping[str, Any]
) -> dict[str, int]:
    """과제별 최소 도구 호출 수(정답 제출 오라클의 호출 수, ADR-0029 §2). 계산하지 못한 과제는 뺀다.

    오라클은 정답을 만드는 과제 유형만 지원한다. 계정이나 정답 대상이 세계에 없으면 건너뛴다."""
    out: dict[str, int] = {}
    accounts = snapshot.get("accounts", {})
    # 오라클은 조회 도구만 불러 세계를 바꾸지 않는다. 깊은 복사가 비싸서 복사본 하나를 공유한다.
    world = base_world.fork(0, clock=FixedClock(tasks[0].as_of_time)) if tasks else None
    for task in tasks:
        account = accounts.get(task.account_id)
        if account is None or world is None:
            continue
        try:
            out[task.task_id] = await min_tool_calls(world, account, task)
        except Exception as exc:  # 계산 실패는 해당 지표만 비운다
            logger.info(
                "최소 호출 수를 계산하지 못했다: task=%s error=%s", task.task_id, type(exc).__name__
            )
    return out


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
    mean_reasoning_tokens: float  # output_tokens의 부분집합
    mean_cost_usd: float | None  # 비용을 모르는 에피소드가 있으면 None
    estimated_episodes: int
    estimated_total_tokens: float
    estimated_total_cost_usd: float | None
    stopped_reason: str | None
    length_finish_rate: float  # 전체 LLM 호출 중 finish_reason == "length" 비율

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
            mean_reasoning_tokens=sum(r.reasoning_tokens for r in results) / count,
            mean_cost_usd=mean_cost,
            estimated_episodes=episodes,
            estimated_total_tokens=(mean_in + mean_out) * episodes,
            estimated_total_cost_usd=mean_cost * episodes if mean_cost is not None else None,
            stopped_reason=summary.stopped_reason,
            length_finish_rate=(
                sum(r.length_finishes for r in results) / max(sum(r.llm_calls for r in results), 1)
            ),
        ),
        summary,
    )
