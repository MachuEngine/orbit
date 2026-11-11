"""Gmail Adapter를 메일 계약 테스트에 등록한다 (conftest 안내 참고).

- `google-mock`: 상태 있는 모의 Gmail(`fake_gmail.py`)로 항상 실행한다. 계정별로 시드(Sim 월드의
  해당 계정 메일함)를 읽어 모의 서버를 새로 만든다.
- `google-live`: 실제 Google 테스트 계정. `ORBIT_CONTRACT_GOOGLE=1`일 때만 등록한다(그 외에는
  등록하지 않아 실행되지 않는다). 방법과 전제는 `docs/connectors/gmail-contract.md`.
"""

import os
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any

import httpx

from orbit.connectors.contracts import MailConnector, PermissionRevoked
from orbit.connectors.google.gmail import GmailConnector
from orbit.connectors.sim.world import SimWorld

from .fake_gmail import FakeGmail, make_transport

FIXED_NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
LIVE_FLAG = "ORBIT_CONTRACT_GOOGLE"
LIVE_TOKEN_ENV = "ORBIT_CONTRACT_GOOGLE_ACCESS_TOKEN"  # noqa: S105 - 환경 변수 이름일 뿐이다


class _StaticTokenProvider:
    def __init__(self, token: str) -> None:
        self._token = token

    async def access_token(self) -> str:
        return self._token

    async def refresh(self) -> str:
        raise PermissionRevoked("테스트용 고정 토큰은 갱신할 수 없다")


def _google_mock(world: SimWorld, account_id: str) -> MailConnector:
    fake = FakeGmail(world.account(account_id), now=FIXED_NOW)
    client = httpx.AsyncClient(
        base_url="https://gmail.mock/gmail/v1", transport=make_transport(fake)
    )
    return GmailConnector(
        _StaticTokenProvider(fake.valid_token), client=client, allow_test_base_url=True
    )


def _google_live(world: SimWorld, account_id: str) -> MailConnector:
    # world는 쓰지 않는다. 시드는 테스트 계정에 미리 만들어 둔다(문서 참고).
    return GmailConnector(_StaticTokenProvider(os.environ[LIVE_TOKEN_ENV]))


def register(calendar: MutableMapping[str, Any], mail: MutableMapping[str, Any]) -> None:
    mail["google-mock"] = _google_mock
    if os.environ.get(LIVE_FLAG) == "1":
        mail["google-live"] = _google_live
