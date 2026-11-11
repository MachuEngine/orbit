"""등록된 구현체가 Protocol 형태를 갖췄는지 확인한다. 등록된 구현체가 없으면 skip된다."""

from orbit.connectors.contracts import CalendarConnector, MailConnector
from orbit.connectors.sim.world import SimWorld

from .conftest import CALENDAR_FACTORIES, MAIL_FACTORIES, CalendarFactory, MailFactory


def test_sim_is_registered_for_calendar_and_mail() -> None:
    """등록 누락으로 계약 테스트가 조용히 0건 실행(전부 skip)되는 것을 막는다.

    "구현체 없음" skip은 레지스트리가 비었을 때의 골격 동작이다. Sim은 항상 등록돼 있어야
    하므로, 누락되면 skip이 아니라 여기서 실패한다.
    """
    assert "sim" in CALENDAR_FACTORIES, "register_sim_calendar.py 등록이 빠졌다"
    assert "sim" in MAIL_FACTORIES, "register_sim_mail.py 등록이 빠졌다"


def test_calendar_factory_builds_connector(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    assert isinstance(calendar_factory(minimal_world, "acct-alice"), CalendarConnector)


def test_mail_factory_builds_connector(mail_factory: MailFactory, minimal_world: SimWorld) -> None:
    assert isinstance(mail_factory(minimal_world, "acct-alice"), MailConnector)
