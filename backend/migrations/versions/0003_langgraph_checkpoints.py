"""LangGraph 체크포인트 테이블 (ADR-0003, ADR-0006, ADR-0012 §1)

Revision ID: 0003
Revises: 0002

테이블은 라이브러리(`langgraph-checkpoint-postgres==3.2.0`, pyproject에서 정확히 고정)가 제공하는
마이그레이션 SQL을 소유자 역할로 그대로 적용한다. 앱 역할이 런타임에 `setup()`을 부르지 않도록
라이브러리의 버전 표(`checkpoint_migrations`)까지 채운다. 라이브러리 버전을 올릴 때는 이 목록이
바뀌었는지 확인하고 새 마이그레이션을 추가한다.

- 이 테이블은 RLS 밖이다(ADR-0006). 그래프 상태에는 ID와 짧은 제어 값만 둔다.
- `CREATE INDEX CONCURRENTLY`는 트랜잭션 안에서 실행할 수 없어 CONCURRENTLY만 뺀다.
- `orbit_app` 권한: 체크포인트 저장(INSERT·UPDATE)과 조회(SELECT), run 삭제 전파(DELETE).
  `checkpoint_migrations`에는 권한을 주지 않는다.
"""

from alembic import op
from langgraph.checkpoint.postgres.base import BasePostgresSaver

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
_EXPECTED_MIGRATIONS = 10  # 라이브러리 3.2.0의 마이그레이션 수. 바뀌면 이 파일을 다시 검토한다.


def upgrade() -> None:
    statements = BasePostgresSaver.MIGRATIONS
    if len(statements) != _EXPECTED_MIGRATIONS:
        raise RuntimeError("langgraph-checkpoint-postgres 마이그레이션 목록이 바뀌었다. 검토 필요")
    for sql in statements:
        op.execute(sql.replace("CONCURRENTLY ", ""))
    # 라이브러리 `setup()`은 버전 표의 최댓값 이후만 실행한다. 이미 적용했음을 기록한다.
    for version in range(len(statements)):
        op.execute(
            f"INSERT INTO checkpoint_migrations (v) VALUES ({int(version)}) ON CONFLICT DO NOTHING"  # noqa: S608
        )
    tables = ", ".join(_TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO orbit_app")  # noqa: S608


def downgrade() -> None:
    for table in (*_TABLES, "checkpoint_migrations"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
