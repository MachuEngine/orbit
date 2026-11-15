"""음성 전사 사용량 테이블 (ADR-0025 §3)

Revision ID: 0011
Revises: 0010

- `voice_usage`: 전사 요청 하나가 한 행이다. 워크스페이스, 시각, 초, 바이트만 둔다. 오디오와
  전사 문장은 저장하지 않는다. 시간당 횟수와 24시간 총 녹음 시간 상한 계산에 쓴다.
- RLS ENABLE + FORCE, `workspace_isolation` 정책. `orbit_app`은 SELECT·INSERT·UPDATE·DELETE
  권한을 갖는다(UPDATE는 제공자가 알려 준 실제 길이로 `seconds`를 보정하는 데, DELETE는 같은
  워크스페이스의 오래된 행 정리에 쓴다).
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE voice_usage (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            at timestamptz NOT NULL,
            seconds double precision NOT NULL CHECK (seconds >= 0),
            bytes integer NOT NULL CHECK (bytes >= 0)
        )
        """
    )
    op.execute("CREATE INDEX voice_usage_workspace_at_idx ON voice_usage (workspace_id, at)")
    expr = "workspace_id = " + _SETTING
    op.execute("ALTER TABLE voice_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE voice_usage FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON voice_usage FOR ALL USING ({expr})"
        f" WITH CHECK ({expr})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON voice_usage TO orbit_app")


def downgrade() -> None:
    op.execute("DROP TABLE voice_usage")
