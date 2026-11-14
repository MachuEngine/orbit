"""prod 시작 점검: DB 역할 격리 조건과 마이그레이션 리비전 (ADR-0022 §3). 실제 PostgreSQL을 쓴다."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from google_mock import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SIGNING_KEY
from orbit.api import startup
from orbit.api.app import create_app
from orbit.api.startup import StartupRefused, code_head_revision, ensure_database_ready
from orbit.core.config import Settings
from orbit.db.session import AsyncDatabase


def _settings(url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": SecretStr(url),
        "env": "prod",
        "google_client_id": CLIENT_ID,
        "google_client_secret": SecretStr(CLIENT_SECRET),
        "google_redirect_uri": REDIRECT_URI,
        "session_signing_key": SecretStr(SIGNING_KEY),
        "login_allowed_emails": "alice@example.test",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
async def owner_db(owner_url: str) -> AsyncIterator[AsyncDatabase]:
    db = AsyncDatabase.from_url(owner_url)
    yield db
    await db.dispose()


async def test_app_role_passes_role_and_revision_checks(
    app_url: str, async_db: AsyncDatabase
) -> None:
    report = await async_db.unscoped_startup_role_report()
    assert tuple(report) == (False, False, False, False)
    assert await async_db.unscoped_alembic_version() == code_head_revision()
    await ensure_database_ready(async_db, _settings(app_url))


async def test_owner_role_is_refused_because_it_owns_rls_tables(
    owner_url: str, owner_db: AsyncDatabase
) -> None:
    report = await owner_db.unscoped_startup_role_report()
    assert report.owns_rls_table is True
    assert report.is_superuser is False and report.bypasses_rls is False
    with pytest.raises(StartupRefused, match="소유") as info:
        await ensure_database_ready(owner_db, _settings(owner_url))
    assert "orbit_owner" not in str(info.value)  # 역할 이름 같은 값은 메시지에 없다


async def test_dev_skips_database_checks(owner_url: str, owner_db: AsyncDatabase) -> None:
    await ensure_database_ready(owner_db, _settings(owner_url, env="dev"))


async def test_revision_mismatch_is_refused(
    app_url: str, async_db: AsyncDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(startup, "code_head_revision", lambda _dir=None: "9999")
    with pytest.raises(StartupRefused, match="리비전") as info:
        await ensure_database_ready(async_db, _settings(app_url))
    assert "9999" not in str(info.value)


async def test_unreadable_revision_is_refused(
    app_url: str, async_db: AsyncDatabase, tmp_path: Path
) -> None:
    # 마이그레이션 디렉터리가 없으면 코드 head를 알 수 없어 거부한다
    with pytest.raises(StartupRefused, match="마이그레이션 디렉터리"):
        await ensure_database_ready(async_db, _settings(app_url), tmp_path / "none")


async def test_lifespan_in_prod_refuses_owner_connection(owner_url: str) -> None:
    app = create_app(_settings(owner_url))
    with pytest.raises(StartupRefused, match="소유"):
        async with app.router.lifespan_context(app):
            pass


async def test_lifespan_in_prod_starts_with_app_role(app_url: str) -> None:
    app = create_app(_settings(app_url))
    async with app.router.lifespan_context(app):
        assert app.state.db is not None
