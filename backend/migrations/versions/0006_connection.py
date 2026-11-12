"""외부 계정 연결 `connection`과 토큰 봉투 암호화 열 (ADR-0016 §2·§3, ADR-0004)

Revision ID: 0006
Revises: 0005

- 로그인 계정과 데이터 연결 계정은 분리한다. 연결은 워크스페이스 범위 테이블이다
  (`workspace_isolation` 정책, FORCE RLS). `(workspace_id, user_id)` 복합 FK로 그 워크스페이스의
  멤버만 연결을 만들 수 있다.
- 토큰은 평문으로 두지 않는다. `token_ciphertext`(AES-256-GCM), `token_nonce`, `wrapped_dek`
  (KEK로 감싼 데이터 키), `kek_id`를 함께 둔다. 네 열은 모두 있거나 모두 없다. `active`는 토큰이
  있어야 하고, `revoked`·`error`는 토큰이 없어야 한다(쓸 수 없는 비밀을 남기지 않는다).
- v0은 워크스페이스당 Google 연결 1개다(`status <> 'revoked'`인 행에 부분 UNIQUE 인덱스).
  `pending`은 허용하지만 v0 흐름은 쓰지 않는다(진행 중 상태는 서명 쿠키에 있다).
- `orbit_app` 권한: SELECT, INSERT, 상태·토큰·시각 열만 UPDATE. DELETE는 workspace 삭제의
  CASCADE로만 일어난다. `account_*`·`scopes`는 바꿀 수 없다(바꾸려면 새 연결을 만든다).
  (0009에서 권한 추가 시 갱신 허용)
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE connection (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            user_id uuid NOT NULL,
            provider text NOT NULL CHECK (provider IN ('google')),
            account_email text NOT NULL,
            account_sub text NOT NULL CHECK (account_sub <> ''),
            scopes text[] NOT NULL,
            status text NOT NULL CHECK (status IN ('pending', 'active', 'revoked', 'error')),
            token_ciphertext bytea,
            token_nonce bytea,
            wrapped_dek bytea,
            kek_id text,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            revoked_at timestamptz,
            FOREIGN KEY (workspace_id, user_id)
                REFERENCES membership (workspace_id, user_id) ON DELETE CASCADE,
            CONSTRAINT connection_token_all_or_none CHECK (
                (token_ciphertext IS NULL) = (token_nonce IS NULL)
                AND (token_nonce IS NULL) = (wrapped_dek IS NULL)
                AND (wrapped_dek IS NULL) = (kek_id IS NULL)
            ),
            CONSTRAINT connection_active_has_token CHECK (
                status <> 'active' OR token_ciphertext IS NOT NULL
            ),
            CONSTRAINT connection_dead_has_no_token CHECK (
                status NOT IN ('revoked', 'error') OR token_ciphertext IS NULL
            ),
            CONSTRAINT connection_revoked_at CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
        )
        """
    )
    op.execute("CREATE INDEX connection_workspace_id_idx ON connection (workspace_id)")
    op.execute(
        "CREATE UNIQUE INDEX connection_one_open_per_provider ON connection"
        " (workspace_id, provider) WHERE status <> 'revoked'"
    )
    expr = "workspace_id = " + _SETTING
    op.execute("ALTER TABLE connection ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connection FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON connection FOR ALL USING ({expr})"
        f" WITH CHECK ({expr})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE (status, token_ciphertext, token_nonce, wrapped_dek,"
        " kek_id, updated_at, revoked_at) ON connection TO orbit_app"
    )


def downgrade() -> None:
    op.execute("DROP TABLE connection")
