"""Sim 캘린더 커넥터를 계약 테스트에 등록한다."""

from collections.abc import MutableMapping
from typing import Any

from orbit.connectors.sim.calendar import SimCalendarConnector
from orbit.connectors.sim.world import SimWorld


def register(calendar: MutableMapping[str, Any], mail: MutableMapping[str, Any]) -> None:
    def factory(world: SimWorld, account_id: str) -> SimCalendarConnector:
        return SimCalendarConnector(world, account_id)

    calendar["sim"] = factory
