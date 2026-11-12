"""FaultPlan: 선언형 장애 주입 규칙 (ADR-0008 5절, D1-4).

규칙은 dict로 읽는다(새 의존성 없음):
    {"rules": [{"tool": "create_event", "nth_call": 1, "error": "result_unknown"}]}

- `nth_call`: 규칙 대상 호출(같은 도구, `match`가 있으면 `match`를 만족하는 호출)의 1부터 센 순번.
  지정하지 않으면 대상 호출마다 적용한다.
- `match`: 호출 인자의 부분 일치(중첩 dict는 재귀적으로 부분 일치, 그 밖은 동등 비교).
  비교 전에 호출 인자와 `match`를 모두 JSON 형태로 정규화한다(`pydantic_core.to_jsonable_python`,
  별칭 사용). aware 시각은 먼저 UTC로 바꾸므로 오프셋이 달라도 같은 시각이면 일치한다.
  시각은 ISO 8601 문자열(UTC는 `Z`), 튜플은 리스트가 된다. 정규화는 해당 도구에 `match`가 있는
  규칙이 있을 때만 한다. 문자열로 쓴 시각은 변환하지 않으므로 UTC `Z` 표기로 쓴다. 따라서 `match`에
  `datetime` 객체를 써도 되고, 문자열로 쓰려면 같은 표기(`2026-11-11T09:00:00Z`)여야 한다.
  리스트는 동등 비교이고 요소 부분 일치는 하지 않는다.
- `result_unknown`은 쓰기를 적용한 뒤 `ResultUnknown`, `result_unknown_not_applied`는 적용하지 않고
  `ResultUnknown`을 던진다.
- 호출 카운터는 `FaultInjector`가 갖는다. 에피소드마다 새 인젝터를 만든다.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import to_jsonable_python

from orbit.connectors.contracts import (
    SCOPE_MISSING_REASON,
    AlreadyExists,
    ConnectorError,
    NotFound,
    PermissionRevoked,
    PreconditionFailed,
    RateLimited,
    ResultUnknown,
    TransientError,
    UnsupportedFile,
)

FaultError = Literal[
    "not_found",
    "already_exists",
    "precondition_failed",
    "rate_limited",
    "permission_revoked",
    "transient",
    "scope_missing",
    "unsupported_file",
    "result_unknown",
    "result_unknown_not_applied",
]


class FaultRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    error: FaultError
    nth_call: int | None = Field(default=None, ge=1)
    match: dict[str, Any] | None = None
    retry_after_s: float | None = None


class FaultPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: tuple[FaultRule, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls.model_validate(dict(data))


def build_error(rule: FaultRule) -> ConnectorError:
    """규칙이 가리키는 예외를 만든다."""
    msg = f"주입된 장애: {rule.error} ({rule.tool})"
    match rule.error:
        case "not_found":
            return NotFound(msg)
        case "already_exists":
            return AlreadyExists(msg)
        case "precondition_failed":
            return PreconditionFailed(msg)
        case "rate_limited":
            return RateLimited(retry_after_s=rule.retry_after_s, message=msg)
        case "permission_revoked":
            return PermissionRevoked(msg)
        case "transient":
            return TransientError(msg)
        case "scope_missing":
            return ConnectorError(msg, reason=SCOPE_MISSING_REASON)
        case "unsupported_file":
            return UnsupportedFile(msg)
        case _:
            return ResultUnknown(msg)


def _to_utc(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", by_alias=True)
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value
    if isinstance(value, Mapping):
        return {k: _to_utc(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_utc(v) for v in value]
    return value


def _normalize(value: Any) -> Any:
    """모델·시각(UTC)·튜플을 JSON 형태(dict·list·str·숫자)로 바꾼다."""
    return to_jsonable_python(_to_utc(value), by_alias=True)


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    return bool(expected == actual)


class FaultInjector:
    """계획과 호출 카운터를 묶는다. 에피소드마다 하나 만든다."""

    def __init__(self, plan: FaultPlan | None = None) -> None:
        self._plan = plan if plan is not None else FaultPlan()
        self._counts = [0] * len(self._plan.rules)

    @property
    def rules(self) -> tuple[FaultRule, ...]:
        return self._plan.rules

    def check(self, tool: str, args: Mapping[str, Any]) -> FaultRule | None:
        """이번 호출에 적용할 규칙을 돌려준다(없으면 `None`). 규칙 순서상 처음 맞는 규칙을 쓴다."""
        chosen: FaultRule | None = None
        normalized: Any = None  # 이 도구에 match 규칙이 있을 때만 한 번 계산한다
        for i, rule in enumerate(self._plan.rules):
            if rule.tool != tool:
                continue
            if rule.match is not None:
                if normalized is None:
                    normalized = _normalize(dict(args))
                if not _subset(_normalize(rule.match), normalized):
                    continue
            self._counts[i] += 1
            if chosen is None and (rule.nth_call is None or rule.nth_call == self._counts[i]):
                chosen = rule
        return chosen
