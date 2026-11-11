"""Sim 메일 커넥터를 계약 테스트에 등록한다 (conftest 안내 참고)."""

from collections.abc import MutableMapping
from typing import Any

from orbit.connectors.contracts import MailConnector
from orbit.connectors.sim.mail import SimMailConnector
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import SimWorld


def _sim_mail(world: SimWorld, account_id: str) -> MailConnector:
    return SimMailConnector(world, account_id, SimRuntime())


def register(calendar: MutableMapping[str, Any], mail: MutableMapping[str, Any]) -> None:
    mail["sim"] = _sim_mail
