import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orbit.actions.executor import ApprovalView
from orbit.actions.hashing import args_hash
from orbit.actions.models import WriteAction
from orbit.actions.store import InMemoryExecutionStore
from orbit.connectors.sim.calendar import SimCalendarConnector
from orbit.connectors.sim.factory import SimConnectorSet, build_sim_connectors
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.mail import SimMailConnector
from orbit.connectors.sim.world import SimWorld, WorldDiff, diff
from orbit.core.clock import FixedClock
from orbit.core.ids import Uuid7Generator

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sim" / "minimal_world.json"
ACCOUNT = "acct-alice"
CALENDAR = "cal-alice-main"
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


@dataclass
class Env:
    world: SimWorld
    before: SimWorld
    conns: SimConnectorSet
    calendar: SimCalendarConnector
    mail: SimMailConnector
    store: InMemoryExecutionStore
    clock: FixedClock

    def approval(self, action: WriteAction, **overrides: Any) -> ApprovalView:
        values: dict[str, Any] = {
            "id": uuid.UUID(int=1),
            "status": "approved",
            "args_hash": args_hash(action),
            "expires_at": NOW + timedelta(minutes=15),
        }
        values.update(overrides)
        return ApprovalView(**values)

    def kwargs(self) -> dict[str, Any]:
        return {
            "calendar": self.calendar,
            "mail": self.mail,
            "store": self.store,
            "clock": self.clock,
        }

    def diff(self) -> WorldDiff:
        return diff(self.before, self.world)

    def calls(self, *tools: str) -> list[str]:
        return [r.tool for r in self.conns.calls.records if not tools or r.tool in tools]

    def sent_with_header(self, execution_hex: str) -> int:
        state = self.world.account(ACCOUNT)
        return sum(
            1
            for m in state.messages.values()
            if "SENT" in m.labels and m.headers.get("X-Orbit-Execution-Id") == execution_hex
        )


@pytest.fixture
def make_env() -> Callable[..., Env]:
    def _make(plan: dict[str, Any] | None = None, seed: int = 1) -> Env:
        clock = FixedClock(NOW)
        world = SimWorld.load(FIXTURE_PATH, clock)
        before = world.copy()
        conns = build_sim_connectors(
            world, [ACCOUNT], FaultPlan.from_dict(plan) if plan is not None else None
        )
        ids = Uuid7Generator(clock=clock, rand=random.Random(seed).getrandbits)  # noqa: S311
        return Env(
            world=world,
            before=before,
            conns=conns,
            calendar=conns.calendar[ACCOUNT],
            mail=conns.mail[ACCOUNT],
            store=InMemoryExecutionStore(ids),
            clock=clock,
        )

    return _make


def faults(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"rules": list(rules)}
