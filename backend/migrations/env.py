"""Alembic 환경. 소유자 역할(`ORBIT_MIGRATION_DATABASE_URL`)로 실행한다.

테스트는 `config.attributes["url"]`로 URL을 직접 넘길 수 있다.
"""

from alembic import context
from sqlalchemy import create_engine

from orbit.core.config import get_settings

config = context.config


def _url() -> str:
    override = config.attributes.get("url")
    if isinstance(override, str):
        return override
    migration_url = get_settings().migration_database_url
    if migration_url is None:
        raise RuntimeError("ORBIT_MIGRATION_DATABASE_URL이 필요하다 (orbit_owner 접속 URL)")
    return migration_url.get_secret_value()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("오프라인 모드는 지원하지 않는다")
run_migrations_online()
