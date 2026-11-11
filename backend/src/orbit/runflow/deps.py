"""그래프·워커가 쓰는 주입 의존성 (ADR-0012 §4).

시계·ID 생성기·LLM·커넥터는 모두 주입한다. 도메인 코드가 `datetime.now()`나 제공자 SDK를 직접
부르지 않는다. 커넥터는 `ConnectorResolver`(워크스페이스 -> (캘린더, 메일))로 받는다. 6단계의
Google Adapter도 이 Protocol로 연결한다.
"""

import uuid
from dataclasses import dataclass, field
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Protocol

from orbit.connectors.contracts import CalendarConnector, MailConnector
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.llm.interface import LLM

DEFAULT_MAX_STEPS = 12  # 한 run의 LLM 호출 상한
DEFAULT_LEASE_SECONDS = 300.0  # 커넥터 호출 타임아웃 x (1 + 재호출 1회)보다 길게(ADR-0012 §3)


class ConnectorResolver(Protocol):
    """워크스페이스의 커넥터를 돌려준다."""

    def connectors_for(
        self, workspace_id: uuid.UUID
    ) -> tuple[CalendarConnector, MailConnector]: ...


class SimConnectorResolver:
    """개발용. 합성 스냅샷을 한 번 읽고, 워크스페이스마다 한 번 포크해 프로세스 안에서 유지한다.

    워크스페이스끼리 월드를 공유하지 않는다(개발용이어도 격리 규칙을 그대로 둔다). 프로세스를 다시
    시작하면 월드는 스냅샷 상태로 돌아간다.
    """

    def __init__(self, snapshot_path: str | Path, account_id: str, clock: Clock) -> None:
        self._base = SimWorld.load(snapshot_path, clock)
        self._account_id = account_id
        self._clock = clock
        self._cache: dict[uuid.UUID, tuple[CalendarConnector, MailConnector]] = {}

    def connectors_for(self, workspace_id: uuid.UUID) -> tuple[CalendarConnector, MailConnector]:
        found = self._cache.get(workspace_id)
        if found is None:
            world = self._base.fork(self._base.seed, self._clock)
            conns = build_sim_connectors(world, [self._account_id])
            found = (conns.calendar[self._account_id], conns.mail[self._account_id])
            self._cache[workspace_id] = found
        return found


@dataclass
class RunDeps:
    db: AsyncDatabase
    clock: Clock
    ids: Uuid7Generator
    llm: LLM
    resolver: ConnectorResolver
    tz: tzinfo
    checkpoint_conninfo: str  # psycopg 접속 문자열(`postgresql://...`). 체크포인터 전용 연결
    approval_ttl: timedelta = field(default_factory=lambda: timedelta(minutes=15))
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    max_steps: int = DEFAULT_MAX_STEPS
