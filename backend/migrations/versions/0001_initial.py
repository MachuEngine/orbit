"""workspace·conversation·message 테이블과 RLS (ADR-0004)

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_USER_TABLES = ("conversation", "message")


def _enable_rls(table: str, expr: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    # 정책 대상은 PUBLIC(TO 생략). USING과 WITH CHECK에 같은 식을 쓴다.
    op.execute(
        f"CREATE POLICY workspace_isolation ON {table} FOR ALL USING ({expr}) WITH CHECK ({expr})"
    )


def upgrade() -> None:
    # 보통 init SQL에서 슈퍼유저가 이미 만든다. 없으면 소유자 권한으로 시도한다.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ID와 created_at은 DB 기본값 없이 앱이 UUIDv7·Clock으로 채운다.
    op.execute(
        """
        CREATE TABLE workspace (
            id uuid PRIMARY KEY,
            created_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE conversation (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL,
            UNIQUE (id, workspace_id)
        )
        """
    )
    # 복합 FK로 메시지가 다른 워크스페이스의 대화를 가리키지 못하게 한다.
    op.execute(
        """
        CREATE TABLE message (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL,
            role text NOT NULL,
            content text NOT NULL,
            created_at timestamptz NOT NULL,
            FOREIGN KEY (conversation_id, workspace_id)
                REFERENCES conversation (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX conversation_workspace_id_idx ON conversation (workspace_id)")
    op.execute(
        "CREATE INDEX message_workspace_conversation_idx ON message (workspace_id, conversation_id)"
    )

    _enable_rls("workspace", "id = " + _SETTING)
    for table in _USER_TABLES:
        _enable_rls(table, "workspace_id = " + _SETTING)

    op.execute("GRANT USAGE ON SCHEMA public TO orbit_app")
    op.execute("GRANT SELECT, INSERT, DELETE ON workspace TO orbit_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conversation, message TO orbit_app")


def downgrade() -> None:
    # 확장(vector)은 다른 객체가 쓸 수 있어 지우지 않는다.
    op.execute("DROP TABLE message")
    op.execute("DROP TABLE conversation")
    op.execute("DROP TABLE workspace")
