"""계약 테스트 공통 골격 (ADR-0008 6절).

구현체 등록 방법 (병렬 작업 사이 충돌을 피하려고 이 파일을 고치지 않는다):
- 이 디렉터리에 `register_<이름>.py`를 만들고 `register(calendar, mail)` 함수를 둔다.
  함수는 받은 딕셔너리에 `이름 -> 팩토리`를 넣는다.
      def register(calendar, mail):
          calendar["sim"] = lambda world, account_id: SimCalendarConnector(world, account_id)
- 팩토리는 `(world: SimWorld, account_id: str)`를 받아 커넥터를 돌려준다. Sim은 world를 쓰고,
  실제 어댑터(6단계)는 world를 무시하고 테스트 계정 연결을 만든다.
- 계약 테스트는 `calendar_factory`, `mail_factory` 픽스처(등록된 구현체마다 한 번씩 실행)와
  `minimal_world`(테스트마다 새로 읽은 월드), `fixed_clock`을 쓴다.
- 등록된 구현체가 없으면 해당 테스트는 "구현체 없음"으로 skip된다.
"""

import importlib
import pkgutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.connectors.contracts import CalendarConnector, MailConnector
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock

CalendarFactory = Callable[[SimWorld, str], CalendarConnector]
MailFactory = Callable[[SimWorld, str], MailConnector]

CALENDAR_FACTORIES: dict[str, CalendarFactory] = {}
MAIL_FACTORIES: dict[str, MailFactory] = {}

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "sim" / "minimal_world.json"
FIXED_NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def _discover_registrations() -> None:
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if info.name.startswith("register_"):
            module = importlib.import_module(f"{__package__}.{info.name}")
            module.register(CALENDAR_FACTORIES, MAIL_FACTORIES)


_discover_registrations()

_NO_IMPL = pytest.param(None, id="no-implementation", marks=pytest.mark.skip(reason="구현체 없음"))


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    for fixture, registry in (
        ("calendar_factory", CALENDAR_FACTORIES),
        ("mail_factory", MAIL_FACTORIES),
    ):
        if fixture in metafunc.fixturenames:
            params = [pytest.param(f, id=name) for name, f in sorted(registry.items())]
            metafunc.parametrize(fixture, params or [_NO_IMPL])


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def minimal_world(fixed_clock: FixedClock) -> SimWorld:
    return SimWorld.load(FIXTURE_PATH, fixed_clock)
