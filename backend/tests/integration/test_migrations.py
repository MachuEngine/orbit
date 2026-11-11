"""마이그레이션 upgrade → downgrade → upgrade. 소유자 역할로 테스트 전용 DB에서 실행한다."""

from alembic import command
from sqlalchemy import create_engine, text

from integration.support import alembic_config, require_test_database


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
    require_test_database(owner_url)  # downgrade가 개발 DB에 실행되는 것을 막는다
    cfg = alembic_config(owner_url)
    expected = {"workspace", "conversation", "message"}

    try:
        command.upgrade(cfg, "head")
        assert _tables(owner_url) == expected

        command.downgrade(cfg, "base")
        assert _tables(owner_url) == set()

        command.upgrade(cfg, "head")
        assert _tables(owner_url) == expected

        engine = create_engine(owner_url)
        try:
            with engine.connect() as conn:
                ext = conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                ).first()
                assert ext is not None
        finally:
            engine.dispose()
    finally:
        # 중간에 실패해도 다른 테스트를 위해 head 상태로 되돌린다.
        command.upgrade(cfg, "head")
