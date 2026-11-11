"""공통 픽스처. 통합 테스트용 DB URL은 환경 변수로 받는다.

- ORBIT_TEST_DATABASE_URL: `orbit_app` 접속 URL (앱 역할)
- ORBIT_TEST_MIGRATION_DATABASE_URL: `orbit_owner` 접속 URL (마이그레이션 테스트용)

값이 없으면 로컬에서는 사유를 출력하고 건너뛴다. `CI=true`이면 실패시킨다.
"""

import os

import pytest


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    reason = (
        f"{name}이 설정되지 않아 통합 테스트를 건너뛴다. "
        "로컬 실행: docker compose -f backend/compose.yml up -d --wait 후 "
        "`set -a; source backend/dev.env; set +a`"
    )
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(
            f"CI에서는 통합 테스트를 건너뛸 수 없다: {name}이 설정되지 않았다", pytrace=False
        )
    pytest.skip(reason)


@pytest.fixture(scope="session")
def app_url() -> str:
    return _require_env("ORBIT_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def owner_url() -> str:
    return _require_env("ORBIT_TEST_MIGRATION_DATABASE_URL")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/integration/" in str(item.path).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)
