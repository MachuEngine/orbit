"""문서(Drive) 계약 테스트의 구현체 등록. 메일·캘린더 `register_*`와 따로 둔다(시그니처 불변).

- `sim`: `SimDriveConnector`. `scope_denied`면 세 메서드 모두에 `scope_missing` 장애를 심는다.
- `google-mock`: 상태 있는 모의 Drive(`fake_drive.py`)를 쓰는 `GoogleDriveConnector`.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from orbit.connectors.contracts import DriveConnector, PermissionRevoked
from orbit.connectors.google.drive import GoogleDriveConnector
from orbit.connectors.sim.drive import SimDriveConnector
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.world import SimWorld

from .fake_drive import FakeDrive, make_transport

FIXED_NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

DriveFactory = Callable[[SimWorld, str, bool], DriveConnector]


class _StaticTokenProvider:
    def __init__(self, token: str) -> None:
        self._token = token

    async def access_token(self) -> str:
        return self._token

    async def refresh(self) -> str:
        raise PermissionRevoked("테스트용 고정 토큰은 갱신할 수 없다")


def _sim(world: SimWorld, account_id: str, scope_denied: bool) -> DriveConnector:
    plan = None
    if scope_denied:
        plan = FaultPlan.from_dict(
            {
                "rules": [
                    {"tool": tool, "error": "scope_missing"}
                    for tool in ("search_files", "get_file", "read_file_text")
                ]
            }
        )
    connector: SimDriveConnector = build_sim_connectors(world, [account_id], plan).drive[account_id]
    return connector


def build_google_mock(
    account_world: SimWorld, account_id: str
) -> tuple[GoogleDriveConnector, FakeDrive]:
    fake = FakeDrive(account_world.account(account_id))
    client = httpx.AsyncClient(
        base_url="https://drive.mock/drive/v3", transport=make_transport(fake)
    )
    connector = GoogleDriveConnector(
        _StaticTokenProvider(fake.valid_token), client=client, allow_test_base_url=True
    )
    return connector, fake


def _google_mock(world: SimWorld, account_id: str, scope_denied: bool) -> DriveConnector:
    connector, fake = build_google_mock(world, account_id)
    fake.scope_missing = scope_denied
    return connector


DRIVE_FACTORIES: dict[str, DriveFactory] = {"sim": _sim, "google-mock": _google_mock}
