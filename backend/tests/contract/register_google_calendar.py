"""Google Calendar 어댑터를 계약 테스트에 등록한다 (ADR-0016 4절).

- `google-mock`: HTTP 모의 서버(`google_calendar_mock.py`)로 항상 실행한다.
  모의 서버는 `minimal_world`의 계정 상태로 시드한다.
- `google-live`: `ORBIT_CONTRACT_GOOGLE=1`과 `ORBIT_CONTRACT_GOOGLE_ACCESS_TOKEN`이 있을 때만
  실행하고, 없으면 skip한다. 실행 방법과 시드 조건은 `docs/connectors/google-contract.md`에 있다.
"""

import os
from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest

from orbit.connectors.google.calendar import GoogleCalendarConnector
from orbit.connectors.sim.world import SimWorld

from .google_calendar_mock import MOCK_BASE_URL, GoogleCalendarMock, mock_transport

LIVE_FLAG = "ORBIT_CONTRACT_GOOGLE"
LIVE_TOKEN = "ORBIT_CONTRACT_GOOGLE_ACCESS_TOKEN"  # noqa: S105  환경 변수 이름이다


class StaticTokens:
    """고정 토큰 공급자. 갱신은 지원하지 않는다."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def access_token(self) -> str:
        return self._token

    async def refresh(self) -> str:
        raise RuntimeError("고정 토큰은 갱신할 수 없다")


def _mock_factory(world: SimWorld, account_id: str) -> GoogleCalendarConnector:
    mock = GoogleCalendarMock.from_account(world.account(account_id), world.clock.now())
    client = httpx.AsyncClient(base_url=MOCK_BASE_URL, transport=mock_transport(mock))
    return GoogleCalendarConnector(
        StaticTokens("mock-token"), client=client, allow_test_base_url=True
    )


def _live_factory(world: SimWorld, account_id: str) -> GoogleCalendarConnector:
    token = os.environ.get(LIVE_TOKEN)
    if os.environ.get(LIVE_FLAG) != "1" or not token:
        pytest.skip(f"실제 Google 실행은 {LIVE_FLAG}=1과 {LIVE_TOKEN}이 있을 때만 한다")
    return GoogleCalendarConnector(StaticTokens(token))


def register(calendar: MutableMapping[str, Any], mail: MutableMapping[str, Any]) -> None:
    calendar["google-mock"] = _mock_factory
    calendar["google-live"] = _live_factory
