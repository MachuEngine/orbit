from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sim" / "minimal_world.json"


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(datetime(2026, 11, 11, 9, 0, tzinfo=UTC))


@pytest.fixture
def world(fixed_clock: FixedClock) -> SimWorld:
    return SimWorld.load(FIXTURE_PATH, fixed_clock)
