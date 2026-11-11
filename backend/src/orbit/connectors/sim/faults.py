"""FaultPlan: 선언형 장애 주입 규칙 (ADR-0008 5절, D1-4).

규칙은 dict로 읽는다(새 의존성 없음):
    {"rules": [{"tool": "create_event", "nth_call": 1, "error": "result_unknown"}]}

- `nth_call`: 규칙 대상 호출(같은 도구, `match`가 있으면 `match`를 만족하는 호출)의 1부터 센 순번.
  지정하지 않으면 대상 호출마다 적용한다.
- `match`: 호출 인자의 부분 일치(중첩 dict는 재귀적으로 부분 일치, 그 밖은 동등 비교).
- `result_unknown`은 쓰기를 적용한 뒤 `ResultUnknown`, `result_unknown_not_applied`는 적용하지 않고
  `ResultUnknown`을 던진다.
- 호출 카운터는 `FaultInjector`가 갖는다. 에피소드마다 새 인젝터를 만든다.
"""

from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from orbit.connectors.contracts import (
    AlreadyExists,
    ConnectorError,
    NotFound,
    PermissionRevoked,
    PreconditionFailed,
    RateLimited,
    ResultUnknown,
    TransientError,
)

FaultError = Literal[
    "not_found",
    "already_exists",
    "precondition_failed",
    "rate_limited",
    "permission_revoked",
    "transient",
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
            return RateLimited(rule.retry_after_s, msg)
        case "permission_revoked":
            return PermissionRevoked(msg)
        case "transient":
            return TransientError(msg)
        case _:
            return ResultUnknown(msg)


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(actual, BaseModel):
        actual = actual.model_dump(mode="python", by_alias=True)
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

    def check(self, tool: str, args: Mapping[str, Any]) -> FaultRule | None:
        """이번 호출에 적용할 규칙을 돌려준다(없으면 `None`). 규칙 순서상 처음 맞는 규칙을 쓴다."""
        chosen: FaultRule | None = None
        for i, rule in enumerate(self._plan.rules):
            if rule.tool != tool:
                continue
            if rule.match is not None and not _subset(rule.match, args):
                continue
            self._counts[i] += 1
            if chosen is None and (rule.nth_call is None or rule.nth_call == self._counts[i]):
                chosen = rule
        return chosen
