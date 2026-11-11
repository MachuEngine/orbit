"""Run·승인·실행 기록 테이블 6개와 Procrastinate 스키마 (ADR-0012 §1, ADR-0005)

Revision ID: 0002
Revises: 0001

orbit_app 권한은 최소로 준다(열 단위 UPDATE). 하위 행 삭제는 `run` 삭제의 ON DELETE CASCADE로만
일어난다(참조 무결성 동작은 테이블 소유자 권한으로 실행되므로 orbit_app에 DELETE를 주지 않는다).
Procrastinate 스키마는 라이브러리가 제공하는 SQL을 그대로 적용한다(버전은 pyproject에서 고정).
"""

from alembic import op
from procrastinate.schema import SchemaManager

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_RECORD_TABLES = ("run", "run_event", "run_message", "tool_call", "approval", "execution")
_PROCRASTINATE_TABLES = (
    "procrastinate_jobs",
    "procrastinate_events",
    "procrastinate_workers",
    "procrastinate_periodic_defers",
)
_PROCRASTINATE_SEQUENCES = (
    "procrastinate_jobs_id_seq",
    "procrastinate_events_id_seq",
    "procrastinate_workers_id_seq",
    "procrastinate_periodic_defers_id_seq",
)


def _enable_rls(table: str) -> None:
    expr = "workspace_id = " + _SETTING
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON {table} FOR ALL USING ({expr}) WITH CHECK ({expr})"
    )


def upgrade() -> None:
    # ID와 시각은 DB 기본값 없이 앱이 UUIDv7·Clock으로 채운다.
    op.execute(
        """
        CREATE TABLE run (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL,
            status text NOT NULL CHECK (status IN
                ('queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled')),
            pattern text NOT NULL,
            config_fingerprint text NOT NULL,
            error_kind text,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            UNIQUE (id, workspace_id),
            FOREIGN KEY (conversation_id, workspace_id)
                REFERENCES conversation (id, workspace_id) ON DELETE CASCADE,
            CHECK (error_kind IS NULL OR status = 'failed')
        )
        """
    )
    # 이벤트·메시지는 run 안에서 seq가 1부터 이어진다. 추가 전용(UPDATE·DELETE 권한 없음).
    op.execute(
        """
        CREATE TABLE run_event (
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            run_id uuid NOT NULL,
            seq bigint NOT NULL CHECK (seq > 0),
            type text NOT NULL,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (run_id, seq),
            FOREIGN KEY (run_id, workspace_id) REFERENCES run (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE run_message (
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            run_id uuid NOT NULL,
            seq bigint NOT NULL CHECK (seq > 0),
            role text NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
            content jsonb NOT NULL,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (run_id, seq),
            FOREIGN KEY (run_id, workspace_id) REFERENCES run (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE tool_call (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            run_id uuid NOT NULL,
            tool_name text NOT NULL,
            args jsonb NOT NULL,
            args_hash text NOT NULL,
            provenance jsonb NOT NULL,
            created_at timestamptz NOT NULL,
            UNIQUE (id, workspace_id),
            FOREIGN KEY (run_id, workspace_id) REFERENCES run (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE approval (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            run_id uuid NOT NULL,
            tool_call_id uuid NOT NULL UNIQUE,
            args_hash text NOT NULL,
            status text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
            expires_at timestamptz NOT NULL,
            decided_at timestamptz,
            created_at timestamptz NOT NULL,
            UNIQUE (id, workspace_id),
            CHECK ((status = 'pending') = (decided_at IS NULL)),
            FOREIGN KEY (run_id, workspace_id) REFERENCES run (id, workspace_id) ON DELETE CASCADE,
            FOREIGN KEY (tool_call_id, workspace_id)
                REFERENCES tool_call (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            approval_id uuid NOT NULL UNIQUE,
            status text NOT NULL CHECK (status IN
                ('pending', 'succeeded', 'failed', 'unknown', 'needs_user')),
            external_ref jsonb,
            error_kind text,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            FOREIGN KEY (approval_id, workspace_id)
                REFERENCES approval (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX run_workspace_conversation_idx ON run (workspace_id, conversation_id)")
    op.execute("CREATE INDEX tool_call_workspace_run_idx ON tool_call (workspace_id, run_id)")
    op.execute("CREATE INDEX approval_workspace_run_idx ON approval (workspace_id, run_id)")

    for table in _RECORD_TABLES:
        _enable_rls(table)

    op.execute(
        "GRANT SELECT, INSERT, DELETE, UPDATE (status, error_kind, updated_at) ON run TO orbit_app"
    )
    op.execute("GRANT SELECT, INSERT ON run_event, run_message, tool_call TO orbit_app")
    op.execute("GRANT SELECT, INSERT, UPDATE (status, decided_at) ON approval TO orbit_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE (status, external_ref, error_kind, updated_at)"
        " ON execution TO orbit_app"
    )

    # Procrastinate. 작업 인자에는 ID만 둔다(ADR-0006). 라이브러리가 쓰는 테이블 전체에 접근을 준다.
    # 스키마 SQL에 '%'가 있어 SQLAlchemy 경유(placeholder 해석) 대신 같은 트랜잭션의 psycopg
    # 연결에 파라미터 없이 직접 실행한다.
    driver_connection = op.get_bind().connection.driver_connection
    assert driver_connection is not None  # noqa: S101
    driver_connection.execute(SchemaManager.get_schema())
    tables = ", ".join(_PROCRASTINATE_TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO orbit_app")
    sequences = ", ".join(_PROCRASTINATE_SEQUENCES)
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequences} TO orbit_app")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
                     WHERE p.pronamespace = 'public'::regnamespace
                       AND p.proname LIKE 'procrastinate\\_%'
            LOOP
                EXECUTE 'DROP FUNCTION ' || r.sig || ' CASCADE';
            END LOOP;
        END $$
        """
    )
    op.execute(f"DROP TABLE {', '.join(_PROCRASTINATE_TABLES)}")
    op.execute("DROP TYPE procrastinate_job_to_defer_v1")
    op.execute("DROP TYPE procrastinate_job_event_type")
    op.execute("DROP TYPE procrastinate_job_status")
    for table in reversed(_RECORD_TABLES):
        op.execute(f"DROP TABLE {table}")
