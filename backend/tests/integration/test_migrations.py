"""마이그레이션 upgrade → downgrade → upgrade. 소유자 역할로 실행한다."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

BACKEND = Path(__file__).resolve().parents[2]


def _config(owner_url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["url"] = owner_url
    return cfg


def _tables(owner_url: str) -> set[str]:
    engine = create_engine(owner_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    " AND tablename IN ('workspace', 'conversation', 'message')"
                )
            )
            return {r[0] for r in rows}
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade(owner_url: str) -> None:
    cfg = _config(owner_url)
    expected = {"workspace", "conversation", "message"}

    command.upgrade(cfg, "head")
    assert _tables(owner_url) == expected

    command.downgrade(cfg, "base")
    assert _tables(owner_url) == set()

    command.upgrade(cfg, "head")
    assert _tables(owner_url) == expected

    engine = create_engine(owner_url)
    try:
        with engine.connect() as conn:
            ext = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first()
            assert ext is not None
    finally:
        engine.dispose()
