"""LLM 인터페이스, 사용량 기록기, 재시도 정책 (ADR-0009 §1).

재시도 정책은 인터페이스가 소유한다. 일시 오류(`TransientLLMError`)만 재시도하고,
재시도에서 쓴 토큰·지연은 최종 응답의 사용량에 합산한다. 기록기는 프롬프트·응답 원문을 받지 않는다.
"""

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage, Purpose


class LLMError(Exception):
    """LLM 호출 오류의 기반. 메시지에 프롬프트·응답 원문·API 키를 넣지 않는다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        # 실패하기까지 재시도에서 쓴 양. 재시도 래퍼가 채운다.
        self.retries: int = 0
        self.usage: LLMUsage = LLMUsage()


class TransientLLMError(LLMError):
    """재시도할 가치가 있는 일시 오류(429, 5xx, 타임아웃, 연결 오류)."""

    def __init__(self, message: str, *, usage: LLMUsage | None = None) -> None:
        super().__init__(message)
        self.attempt_usage: LLMUsage = usage or LLMUsage()


class LLMTimeoutError(TransientLLMError):
    pass


class LLMRequestError(LLMError):
    """4xx처럼 재시도해도 같은 결과가 나오는 오류."""


class ModelMismatchError(LLMError):
    """실제 응답 모델·제공자가 설정과 다르다."""


class MalformedResponseError(LLMError):
    """응답 형식이 기대와 다르다(도구 인자가 JSON이 아님 등)."""


@runtime_checkable
class LLM(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2  # 첫 시도 제외 추가 시도 횟수
    backoff_seconds: float = 0.5  # 첫 재시도 전 대기. 이후 2배씩 늘린다(지터 없음: 결정적)

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.backoff_seconds < 0:
            raise ValueError("재시도 설정은 0 이상이어야 한다")


async def run_with_retry(
    call: Callable[[], Awaitable[LLMResponse]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.perf_counter,
) -> LLMResponse:
    """`call`을 부르고 일시 오류만 재시도한다. 사용량·지연은 시도 전체를 합산한다."""
    started = monotonic()
    extra_usage = LLMUsage()
    retries = 0
    while True:
        try:
            response = await call()
        except TransientLLMError as exc:
            extra_usage = extra_usage + exc.attempt_usage
            if retries >= policy.max_retries:
                exc.retries = retries
                exc.usage = extra_usage
                raise
            await sleep(policy.backoff_seconds * (2**retries))
            retries += 1
            continue
        except LLMError as exc:
            exc.retries = retries
            exc.usage = extra_usage
            raise
        elapsed_ms = round((monotonic() - started) * 1000)
        return dataclasses.replace(
            response,
            usage=response.usage + extra_usage,
            latency_ms=elapsed_ms,
            retries=retries,
        )


@dataclass(frozen=True)
class UsageRecord:
    """호출 한 건의 측정값. 프롬프트·응답 원문 필드는 일부러 없다."""

    purpose: Purpose
    model_id: str | None  # 실패해서 응답이 없으면 None
    provider: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float | None
    latency_ms: int | None
    retries: int
    error_type: str | None = None  # 실패 시 예외 클래스 이름만


class UsageRecorder:
    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    @property
    def records(self) -> tuple[UsageRecord, ...]:
        return tuple(self._records)

    def record(self, record: UsageRecord) -> None:
        self._records.append(record)

    @property
    def total_usage(self) -> LLMUsage:
        total = LLMUsage()
        for r in self._records:
            total = total + LLMUsage(r.input_tokens, r.output_tokens, r.cached_input_tokens)
        return total

    @property
    def total_cost_usd(self) -> float | None:
        """모든 성공 호출의 비용 합. 하나라도 비용을 모르면 None(과소 집계를 숨기지 않는다)."""
        if any(r.cost_usd is None and r.error_type is None for r in self._records):
            return None
        return sum(r.cost_usd for r in self._records if r.cost_usd is not None)


class RecordingLLM:
    """어떤 `LLM`이든 감싸서 호출마다 `UsageRecorder`에 기록한다."""

    def __init__(self, inner: LLM, recorder: UsageRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            response = await self._inner.complete(request)
        except LLMError as exc:
            self._recorder.record(
                UsageRecord(
                    purpose=request.purpose,
                    model_id=None,
                    provider=None,
                    input_tokens=exc.usage.input_tokens,
                    output_tokens=exc.usage.output_tokens,
                    cached_input_tokens=exc.usage.cached_input_tokens,
                    cost_usd=None,
                    latency_ms=None,
                    retries=exc.retries,
                    error_type=type(exc).__name__,
                )
            )
            raise
        self._recorder.record(
            UsageRecord(
                purpose=request.purpose,
                model_id=response.model_id,
                provider=response.provider,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cached_input_tokens=response.usage.cached_input_tokens,
                cost_usd=response.cost_usd,
                latency_ms=response.latency_ms,
                retries=response.retries,
            )
        )
        return response
