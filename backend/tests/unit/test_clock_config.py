from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from orbit.core.clock import Clock, FixedClock, SystemClock
from orbit.core.config import Settings


def test_fixed_clock_does_not_move() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FixedClock(at)
    assert clock.now() == at
    assert clock.now() == at
    clock.advance(timedelta(hours=1))
    assert clock.now() == at + timedelta(hours=1)


def test_fixed_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="시간대"):
        FixedClock(datetime(2026, 1, 1))  # noqa: DTZ001 - 거부를 확인하려는 의도적 입력


def test_clocks_satisfy_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FixedClock(datetime(2026, 1, 1, tzinfo=UTC)), Clock)
    assert SystemClock().now().tzinfo is not None


def test_settings_reads_prefixed_env_and_hides_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_DATABASE_URL", "postgresql+psycopg://u:pw@h/db")
    monkeypatch.delenv("ORBIT_MIGRATION_DATABASE_URL", raising=False)
    settings = Settings()
    assert settings.database_url.get_secret_value().endswith("@h/db")
    assert "pw" not in repr(settings)
    assert settings.migration_database_url is None


def test_settings_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORBIT_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
