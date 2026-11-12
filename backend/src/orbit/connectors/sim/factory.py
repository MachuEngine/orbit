"""Sim 커넥터 묶음 팩토리. 4단계 쓰기 경로와 평가 러너는 이 팩토리만 쓴다.

메일·캘린더·문서 커넥터가 하나의 공유 `SimRuntime`(하나의 FaultPlan, 하나의 호출 기록)을 쓰게 하고,
모든 커넥터를 만든 뒤 런타임을 `seal()`한다. 어느 커넥터에도 속하지 않는 규칙(오타 등)은
`ValueError`로 알려 규칙이 조용히 무시되지 않게 한다. 커넥터와 `SimRuntime`을 직접 조립하지 않는다.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from orbit.connectors.sim.calendar import SimCalendarConnector
from orbit.connectors.sim.calls import CallLog
from orbit.connectors.sim.drive import SimDriveConnector
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.mail import SimMailConnector
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import SimWorld


@dataclass(frozen=True)
class SimConnectorSet:
    runtime: SimRuntime
    mail: dict[str, SimMailConnector]  # account_id -> 커넥터
    calendar: dict[str, SimCalendarConnector]
    drive: dict[str, SimDriveConnector]

    @property
    def calls(self) -> CallLog:
        return self.runtime.calls


def build_sim_connectors(
    world: SimWorld, account_ids: Iterable[str], plan: FaultPlan | None = None
) -> SimConnectorSet:
    """계정마다 메일·캘린더·문서 커넥터를 만들고 공유 런타임을 봉인한다.

    `plan`의 규칙이 어느 커넥터에도 속하지 않는 `tool`을 가리키면 `ValueError`다. `account_id`
    match로 계정별 장애를 지정할 수 있다.
    """
    ids = list(account_ids)
    runtime = SimRuntime(plan, shared=True)
    mail = {a: SimMailConnector(world, a, runtime) for a in ids}
    calendar = {a: SimCalendarConnector(world, a, runtime) for a in ids}
    drive = {a: SimDriveConnector(world, a, runtime) for a in ids}
    runtime.seal()
    return SimConnectorSet(runtime=runtime, mail=mail, calendar=calendar, drive=drive)
