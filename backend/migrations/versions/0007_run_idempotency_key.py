"""`POST /runs` 멱등 키: `run.idempotency_key`, `run.request_hash`

Revision ID: 0007
Revises: 0006

- 응답이 유실돼 클라이언트가 같은 요청을 다시 보내도 run을 하나만 만든다.
- `(workspace_id, idempotency_key)`는 키가 있는 행에서만 유일하다(키가 없는 기존 run은 영향 없음).
- `request_hash`는 메시지와 대화 ID의 SHA-256이다. 같은 키에 다른 요청이 오면 거절하는 데 쓴다.
- 새 열은 `run`의 기존 테이블 권한(SELECT·INSERT)과 RLS 정책을 그대로 따른다. UPDATE 열 권한은
  늘리지 않는다(생성 때만 쓰고 바꾸지 않는다).
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE run ADD COLUMN idempotency_key uuid, ADD COLUMN request_hash text")
    op.execute(
        "ALTER TABLE run ADD CONSTRAINT run_idempotency_hash CHECK"
        " (idempotency_key IS NULL OR request_hash IS NOT NULL)"
    )
    op.execute(
        "CREATE UNIQUE INDEX run_workspace_idempotency_key ON run (workspace_id, idempotency_key)"
        " WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX run_workspace_idempotency_key")
    op.execute("ALTER TABLE run DROP CONSTRAINT run_idempotency_hash")
    op.execute("ALTER TABLE run DROP COLUMN request_hash, DROP COLUMN idempotency_key")
