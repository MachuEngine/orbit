"""Sim 커넥터 공통 실행 래퍼. 장애 주입과 호출 기록을 한곳에서 일관되게 처리한다.

사용 예 (커넥터 메서드 안):

    async def create_event(self, calendar_id, event_id, draft):
        args = {"calendar_id": calendar_id, "event_id": event_id, "draft": draft}
        return await self._rt.run_tool("create_event", args, lambda: self._apply_create(...))
"""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, overload

from orbit.connectors.contracts import CALENDAR_TOOLS, DRIVE_TOOLS, MAIL_TOOLS
from orbit.connectors.sim.calls import CallLog
from orbit.connectors.sim.faults import FaultInjector, FaultPlan, build_error


class SimRuntime:
    """장애 주입기와 호출 기록을 묶는다. 커넥터는 생성 때 `bind`로 자기 도구 이름을 등록한다.

    - 기본(`shared=False`): 런타임은 커넥터 하나 전용이다. 규칙의 `tool`이 그 커넥터의 Protocol
      메서드 이름이 아니면 `bind`가 `ValueError`를 던진다(오타로 규칙이 조용히 무시되는 것 방지).
    - `shared=True`: 메일·캘린더 커넥터가 한 런타임(하나의 FaultPlan, 하나의 호출 기록)을 공유한다.
      `bind` 시점에는 규칙의 `tool`이 계약 전체(캘린더 ∪ 메일 ∪ 문서) 도구 이름이 아니면
      `ValueError`다.
      바인딩 순서와 무관하게 하려고 개별 `bind`는 이 느슨한 검사만 하고, 모든 커넥터를 만든 뒤
      `check_bound()`를 부르면 어느 커넥터에도 속하지 않는 규칙을 `ValueError`로 알린다.
      공유 런타임은 `seal()`로 봉인하기 전에는 `run_tool`을 거절하고(`RuntimeError`), 봉인 뒤에는
      `bind`를 거절한다. `seal()`이 `check_bound()`를 수행하므로 검사 누락으로 규칙이 조용히
      무시될 수 없다. 직접 만들지 말고 `build_sim_connectors`(sim/factory.py)를 쓴다.
    """

    def __init__(
        self, faults: FaultPlan | FaultInjector | None = None, *, shared: bool = False
    ) -> None:
        self.faults = faults if isinstance(faults, FaultInjector) else FaultInjector(faults)
        self.calls = CallLog()
        self.shared = shared
        self._bound: set[str] = set()
        self._sealed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    def seal(self) -> None:
        """공유 런타임: `check_bound()`를 수행하고 봉인한다. 봉인 전에는 `run_tool`을 쓸 수 없다."""
        self.check_bound()
        self._sealed = True

    def bind(self, tools: frozenset[str]) -> None:
        """커넥터가 제공하는 도구 이름을 등록하고 규칙의 `tool`을 검증한다."""
        if self._sealed:
            raise RuntimeError("봉인된 런타임에는 커넥터를 더 바인딩할 수 없다")
        allowed = (CALENDAR_TOOLS | MAIL_TOOLS | DRIVE_TOOLS) if self.shared else tools
        for rule in self.faults.rules:
            if rule.tool not in allowed:
                raise ValueError(
                    f"FaultPlan 규칙의 tool {rule.tool!r}이 커넥터 메서드가 아니다: "
                    f"{sorted(allowed)}"
                )
        self._bound |= tools

    def check_bound(self) -> None:
        """모든 규칙의 `tool`이 바인딩된 커넥터 중 하나의 메서드인지 확인한다."""
        for rule in self.faults.rules:
            if rule.tool not in self._bound:
                raise ValueError(f"FaultPlan 규칙의 tool {rule.tool!r}을 가진 커넥터가 없다")

    @overload
    async def run_tool[T](
        self,
        tool: str,
        args: Mapping[str, Any],
        apply_fn: Callable[[], Awaitable[T]],
        *,
        account_id: str | None = None,
    ) -> T: ...

    @overload
    async def run_tool[T](
        self,
        tool: str,
        args: Mapping[str, Any],
        apply_fn: Callable[[], T],
        *,
        account_id: str | None = None,
    ) -> T: ...

    async def run_tool(
        self,
        tool: str,
        args: Mapping[str, Any],
        apply_fn: Callable[[], Any],
        *,
        account_id: str | None = None,
    ) -> Any:
        """(1) 장애 규칙 확인 (2) `apply_fn` 실행 (3) 결과 불명 처리 (4) 호출 기록.

        - 적용 전 오류와 `result_unknown_not_applied`는 `apply_fn`을 실행하지 않고 raise한다.
        - `result_unknown`은 `apply_fn`을 실행해 상태를 바꾼 뒤 `ResultUnknown`을 raise한다.
        - `apply_fn`이 던진 예외는 기록한 뒤 그대로 다시 던진다.
        - `account_id`가 있으면 `FaultPlan`의 `match`에서 `account_id` 키로 비교할 수 있다
          (호출 기록의 인자에는 넣지 않는다). 런타임을 공유하는 커넥터를 계정별로 구분한다.
        """
        if self.shared and not self._sealed:
            raise RuntimeError("공유 런타임은 seal() 뒤에 써야 한다(build_sim_connectors 사용)")
        match_args = args if account_id is None else {**args, "account_id": account_id}
        rule = self.faults.check(tool, match_args)
        if rule is not None and rule.error != "result_unknown":
            pre_err = build_error(rule)
            self.calls.record(tool, args, outcome="error", error_type=type(pre_err).__name__)
            raise pre_err

        try:
            result: Any = apply_fn()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self.calls.record(tool, args, outcome="error", error_type=type(exc).__name__)
            raise

        if rule is not None:  # result_unknown: 적용은 됐지만 응답이 유실됐다
            err = build_error(rule)
            self.calls.record(tool, args, outcome="error", error_type=type(err).__name__)
            raise err

        self.calls.record(tool, args, outcome="ok", result=result)
        return result
