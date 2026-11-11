"""에피소드 계약: 평가 대상(SUT) 인터페이스, 예산, 단계 카운터 (ADR-0009 §3).

평가 대상이 보고하는 값은 채점에 쓰지 않는다. 궤적·상태 변화·사용량은 러너가 Sim 호출 기록과
`UsageRecorder`에서 직접 읽는다. `EpisodeOutput`은 최종 응답만 담는다.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from orbit.connectors.contracts import CalendarConnector, MailConnector
from orbit.connectors.sim.calls import CallLog
from orbit.core.clock import Clock
from orbit.llm.interface import LLM, RecordingLLM, UsageRecord, UsageRecorder
from orbit.llm.types import LLMRequest, LLMResponse


@dataclass(frozen=True)
class Budget:
    """에피소드 상한. 하나라도 넘으면 `budget_exceeded`로 중단한다."""

    # 단계 = 러너가 관찰한 LLM 호출 수 + 커넥터 호출 수. 평가 대상이 보고한 횟수는 세지 않는다.
    max_steps: int = 50
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
    """단계 한도 검사기. 단계는 러너가 관찰한 값으로만 센다.

    단계 = LLM 호출 수(`UsageRecorder`) + 커넥터 호출 수(Sim 호출 기록). 평가 대상이 보고한
    횟수는 세지 않는다. 도구 하나가 커넥터 호출을 여러 개 만들 수 있고(예: 모든 캘린더 조회),
    그 호출은 모두 단계로 센다.

    - `consume()`: 다음 동작 1단계를 더해도 한도 이내인지 검사한다. 넘으면 `BudgetExceeded`.
      상태를 바꾸지 않으므로 평가 대상이 동작 직전에 불러야 한다.
    - `has_room()`: `consume()`과 같은 검사를 예외 없이 한다.
    - `verify()`: 에피소드 뒤에 러너가 쓴다. 이미 쓴 단계·토큰이 한도를 넘었는지 본다.
    """

    def __init__(
        self,
        budget: Budget,
        recorder: UsageRecorder,
        calls: CallLog,
        *,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._budget = budget
        self._recorder = recorder
        self._calls = calls
        self._monotonic = monotonic
        self._started = monotonic()

    @property
    def steps(self) -> int:
        """지금까지 쓴 단계: LLM 호출 + 커넥터 호출."""
        return len(self._recorder.records) + len(self._calls.records)

    def _violation(
        self, extra: int, *, check_time: bool
    ) -> Literal["steps", "tokens", "time"] | None:
        if self.steps + extra > self._budget.max_steps:
            return "steps"
        usage = self._recorder.total_usage
        if usage.input_tokens + usage.output_tokens > self._budget.max_tokens:
            return "tokens"
        if check_time and self._monotonic() - self._started > self._budget.max_seconds:
            return "time"
        return None

    def has_room(self) -> bool:
        return self._violation(1, check_time=True) is None

    def consume(self) -> None:
        reason = self._violation(1, check_time=True)
        if reason is not None:
            raise BudgetExceeded(reason)

    def verify(self) -> Literal["steps", "tokens"] | None:
        """에피소드 뒤 재검사. 시간은 러너의 타임아웃이 따로 막는다."""
        reason = self._violation(0, check_time=False)
        return reason if reason in ("steps", "tokens") else None


@dataclass(frozen=True)
class SutTask:
    """평가 대상에게 보이는 과제. 정답(`gold`)과 채점 단서, 분할·태그·난이도는 없다."""

    task_id: str
    turns: tuple["SutTurn", ...]
    as_of_time: datetime
    account_id: str


@dataclass(frozen=True)
class SutTurn:
    role: str
    content: str


class ReadOnlyClock:
    """`now()`만 있는 시계. 평가 대상이 시각을 옮기지 못하게 한다."""

    def __init__(self, inner: Clock) -> None:
        self._now = inner.now

    def now(self) -> datetime:
        return self._now()


@dataclass(frozen=True)
class EpisodeConnectors:
    """과제 계정 하나의 메일·캘린더 커넥터. 런타임·호출 기록·FaultPlan은 노출하지 않는다."""

    mail: MailConnector
    calendar: CalendarConnector


class EpisodeRecordingLLM(RecordingLLM):
    """`RecordingLLM`에 더해, 취소(시간 초과)된 진행 중 호출을 "취소됨, 비용 미상"으로 남긴다.

    취소된 호출은 토큰과 비용을 알 수 없다. `cancelled`가 0보다 크면 러너가 에피소드 비용을
    `None`으로 기록해 누적 비용 상한이 토큰 상한 대체 경로를 타게 한다.
    """

    def __init__(self, inner: LLM, recorder: UsageRecorder) -> None:
        super().__init__(inner, recorder)
        self._rec = recorder
        self.cancelled = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            return await super().complete(request)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                self.cancelled += 1
                self._rec.record(
                    UsageRecord(
                        purpose=request.purpose,
                        model_id=None,
                        provider=None,
                        input_tokens=0,
                        output_tokens=0,
                        cached_input_tokens=0,
                        cost_usd=None,
                        latency_ms=None,
                        retries=0,
                        error_type="Cancelled",
                    )
                )
            raise


@dataclass(frozen=True)
class Episode:
    """에피소드 하나에 평가 대상이 쓸 수 있는 것. 정답·FaultPlan·호출 기록은 숨긴다."""

    task: SutTask
    connectors: EpisodeConnectors
    clock: Clock  # 과제 `as_of_time`에 고정된 읽기 전용 시계
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
