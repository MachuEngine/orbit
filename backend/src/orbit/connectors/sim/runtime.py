"""Sim 커넥터 공통 실행 래퍼. 장애 주입과 호출 기록을 한곳에서 일관되게 처리한다.

사용 예 (커넥터 메서드 안):

    async def create_event(self, calendar_id, event_id, draft):
        args = {"calendar_id": calendar_id, "event_id": event_id, "draft": draft}
        return await self._rt.run_tool("create_event", args, lambda: self._apply_create(...))
"""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, overload

from orbit.connectors.sim.calls import CallLog
from orbit.connectors.sim.faults import FaultInjector, FaultPlan, build_error


class SimRuntime:
    def __init__(self, faults: FaultPlan | FaultInjector | None = None) -> None:
        self.faults = faults if isinstance(faults, FaultInjector) else FaultInjector(faults)
        self.calls = CallLog()

    @overload
    async def run_tool[T](
        self, tool: str, args: Mapping[str, Any], apply_fn: Callable[[], Awaitable[T]]
    ) -> T: ...

    @overload
    async def run_tool[T](
        self, tool: str, args: Mapping[str, Any], apply_fn: Callable[[], T]
    ) -> T: ...

    async def run_tool(
        self, tool: str, args: Mapping[str, Any], apply_fn: Callable[[], Any]
    ) -> Any:
        """(1) 장애 규칙 확인 (2) `apply_fn` 실행 (3) 결과 불명 처리 (4) 호출 기록.

        - 적용 전 오류와 `result_unknown_not_applied`는 `apply_fn`을 실행하지 않고 raise한다.
        - `result_unknown`은 `apply_fn`을 실행해 상태를 바꾼 뒤 `ResultUnknown`을 raise한다.
        - `apply_fn`이 던진 예외는 기록한 뒤 그대로 다시 던진다.
        """
        rule = self.faults.check(tool, args)
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
