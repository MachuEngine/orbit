"""에피소드 계약: 평가 대상(SUT) 인터페이스, 예산, 단계 카운터 (ADR-0009 §3).

평가 대상이 보고하는 값은 채점에 쓰지 않는다. 궤적·상태 변화·사용량은 러너가 Sim 호출 기록과
`UsageRecorder`에서 직접 읽는다. `EpisodeOutput`은 최종 응답만 담는다.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from orbit.connectors.sim.factory import SimConnectorSet
from orbit.core.clock import Clock
from orbit.evalrunner.task_source import TaskView
from orbit.llm.interface import RecordingLLM, UsageRecorder


@dataclass(frozen=True)
class Budget:
    """에피소드 상한. 하나라도 넘으면 `budget_exceeded`로 중단한다."""

    max_steps: int = 50  # 도구 호출 + LLM 호출
    max_tokens: int = 200_000  # 입력 + 출력 토큰
    max_seconds: float = 120.0  # 벽시계 시간

    def __post_init__(self) -> None:
        if self.max_steps < 1 or self.max_tokens < 1 or self.max_seconds <= 0:
            raise ValueError("예산 상한은 양수여야 한다")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
        }


class BudgetExceeded(Exception):  # noqa: N818  도메인 용어
    """예산 상한 초과. `reason`은 `steps` | `tokens` | `time`."""

    def __init__(self, reason: Literal["steps", "tokens", "time"]) -> None:
        super().__init__(f"예산 초과: {reason}")
        self.reason = reason


class StepCounter:
    """평가 대상이 도구 호출·LLM 호출마다 `consume()`을 부른다. 상한을 넘기면 예외를 던진다.

    평가 대상이 부르지 않아도 러너가 에피소드 뒤에 호출 기록으로 단계·토큰을 다시 검사한다.
    """

    def __init__(
        self,
        budget: Budget,
        recorder: UsageRecorder,
        *,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._budget = budget
        self._recorder = recorder
        self._monotonic = monotonic
        self._started = monotonic()
        self.steps = 0

    def consume(self) -> None:
        if self.steps + 1 > self._budget.max_steps:
            raise BudgetExceeded("steps")
        usage = self._recorder.total_usage
        if usage.input_tokens + usage.output_tokens > self._budget.max_tokens:
            raise BudgetExceeded("tokens")
        if self._monotonic() - self._started > self._budget.max_seconds:
            raise BudgetExceeded("time")
        self.steps += 1


@dataclass(frozen=True)
class Episode:
    """에피소드 하나에 평가 대상이 쓸 수 있는 것. FaultPlan은 숨긴다(커넥터 안에서만 작동)."""

    task: TaskView
    connectors: SimConnectorSet  # 과제 계정 하나의 메일·캘린더 커넥터
    clock: Clock  # 과제 `as_of_time`에 고정된 에피소드 전용 시계
    llm: RecordingLLM
    budget: Budget
    steps: StepCounter


@dataclass(frozen=True)
class EpisodeOutput:
    final_text: str
    structured: dict[str, Any] | None = None


class SystemUnderTest(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def version(self) -> str: ...

    async def run(self, episode: Episode) -> EpisodeOutput: ...
