"""등록된 구현체가 Protocol 형태를 갖췄는지 확인한다. 등록된 구현체가 없으면 skip된다."""

from orbit.connectors.contracts import CalendarConnector, MailConnector
from orbit.connectors.sim.world import SimWorld

from .conftest import CalendarFactory, MailFactory


def test_calendar_factory_builds_connector(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    assert isinstance(calendar_factory(minimal_world, "acct-alice"), CalendarConnector)


def test_mail_factory_builds_connector(mail_factory: MailFactory, minimal_world: SimWorld) -> None:
    assert isinstance(mail_factory(minimal_world, "acct-alice"), MailConnector)
