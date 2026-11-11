"""쿠키 세션 테이블 `auth_session`과 조회 함수 (ADR-0013 §2, ADR-0004)

Revision ID: 0004
Revises: 0003

- 토큰 원문은 저장하지 않는다. `token_hash`(SHA-256 hex)와 `csrf_token_hash`만 둔다.
- 테이블은 다른 사용자 테이블처럼 RLS·FORCE RLS·`workspace_isolation` 정책을 쓴다.
- 쿠키로 세션을 찾는 시점에는 워크스페이스를 모른다. 그래서 `auth_session_lookup(token_hash)`
  하나만 `SECURITY DEFINER`로 만들어 `orbit_app`에 EXECUTE를 연다. 함수 소유자(마이그레이션을 실행한
  역할)에게만 적용되는 SELECT 정책(`session_lookup`)이 있어 함수 안에서만 RLS를 통과한다.
  `orbit_app`이 테이블을 직접 읽을 때는 `workspace_isolation`만 적용된다.
- 함수는 `search_path`를 고정하고 `token_hash` 일치 행만 돌려준다(UNIQUE라 최대 한 행).
- `orbit_app` 권한: SELECT, INSERT, `revoked_at`만 UPDATE.
  DELETE는 workspace 삭제의 CASCADE로만 일어난다.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_session (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            csrf_token_hash text NOT NULL CHECK (csrf_token_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX auth_session_workspace_id_idx ON auth_session (workspace_id)")
    expr = "workspace_id = " + _SETTING
    op.execute("ALTER TABLE auth_session ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auth_session FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON auth_session FOR ALL USING ({expr})"
        f" WITH CHECK ({expr})"
    )
    # 함수 소유자(SECURITY DEFINER 실행 중의 current_user)에게만 적용되는 조회 정책
    op.execute(
        "CREATE POLICY session_lookup ON auth_session FOR SELECT TO CURRENT_USER USING (true)"
    )
    op.execute(
        """
        CREATE FUNCTION auth_session_lookup(p_token_hash text)
        RETURNS TABLE (
            session_id uuid,
            workspace_id uuid,
            csrf_token_hash text,
            expires_at timestamptz,
            revoked_at timestamptz
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT s.id, s.workspace_id, s.csrf_token_hash, s.expires_at, s.revoked_at
            FROM public.auth_session AS s
            WHERE s.token_hash = p_token_hash
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION auth_session_lookup(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION auth_session_lookup(text) TO orbit_app")
    op.execute("GRANT SELECT, INSERT, UPDATE (revoked_at) ON auth_session TO orbit_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION auth_session_lookup(text)")
    op.execute("DROP TABLE auth_session")
