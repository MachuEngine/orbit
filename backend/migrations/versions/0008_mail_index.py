"""메일 색인 테이블 4개와 주기 작업용 조회 함수 (ADR-0019 §3)

Revision ID: 0008
Revises: 0007

- 네 테이블 모두 `workspace_id` FK, `(connection_id, workspace_id)` 복합 FK(`connection` 삭제 시
  CASCADE), RLS ENABLE + FORCE, `workspace_isolation` 정책을 쓴다. 복합 FK를 위해 `connection`에
  `UNIQUE (id, workspace_id)`를 더한다(기존 `PRIMARY KEY (id)`는 그대로).
- `chunk`는 `(source_item_id, workspace_id)` 복합 FK로 `source_item` 삭제 시 CASCADE 된다.
  `embedding vector(1536)`은 HNSW(코사인), `tokens tsvector`는 GIN 인덱스를 둔다.
- 주기 작업은 워크스페이스를 모르는 상태에서 대상을 골라야 한다. `mail_index_due_connections`
  (`SECURITY DEFINER`)가 `(workspace_id, connection_id)`만 돌려준다. 마지막 동기화가
  `p_stale_before`보다 오래됐거나 아직 없는 활성 연결이 대상이다. 오류 상태는 영구 오류
  (`permission_revoked`)면 제외하고, 그 밖의 오류는 마지막 시도가 `p_error_retry_before`보다
  오래됐을 때만 다시 고른다(백오프). 함수 소유자에게만 적용되는
  `due_lookup` SELECT 정책이 `connection`·`index_scope`·`sync_state`에 있어 함수 안에서만 RLS를
  통과한다(0004와 같은 방식). 제목·본문·주소는 돌려주지 않는다.
- `orbit_app` 권한: 네 테이블 SELECT·INSERT·UPDATE·DELETE. 연결 해제·범위 삭제·동기화가 직접
  지운다(소프트 삭제 없음).
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_TABLES = ("index_scope", "sync_state", "source_item", "chunk")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE connection"
        " ADD CONSTRAINT connection_id_workspace_key UNIQUE (id, workspace_id)"
    )

    op.execute(
        """
        CREATE TABLE index_scope (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            connection_id uuid NOT NULL,
            label_id text NOT NULL CHECK (label_id <> ''),
            label_name text NOT NULL,
            period_days integer NOT NULL CHECK (period_days IN (30, 90, 365)),
            initial_synced_at timestamptz,
            created_at timestamptz NOT NULL,
            UNIQUE (connection_id, label_id),
            UNIQUE (id, workspace_id),
            FOREIGN KEY (connection_id, workspace_id)
                REFERENCES connection (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sync_state (
            connection_id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            history_id text,
            last_synced_at timestamptz,
            status text NOT NULL CHECK (status IN ('pending', 'ok', 'error')),
            error_kind text,
            updated_at timestamptz NOT NULL,
            FOREIGN KEY (connection_id, workspace_id)
                REFERENCES connection (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE source_item (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            connection_id uuid NOT NULL,
            kind text NOT NULL CHECK (kind <> ''),
            provider_item_id text NOT NULL CHECK (provider_item_id <> ''),
            thread_id text NOT NULL,
            subject text NOT NULL,
            sender_name text,
            sender_email text NOT NULL,
            sent_at timestamptz NOT NULL,
            label_ids text[] NOT NULL,
            body_hash text NOT NULL,
            indexed_at timestamptz NOT NULL,
            UNIQUE (connection_id, provider_item_id),
            UNIQUE (id, workspace_id),
            FOREIGN KEY (connection_id, workspace_id)
                REFERENCES connection (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE chunk (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            connection_id uuid NOT NULL,
            source_item_id uuid NOT NULL,
            ord integer NOT NULL CHECK (ord >= 0),
            content text NOT NULL,
            tokens tsvector NOT NULL,
            embedding vector(1536) NOT NULL,
            embedding_model text NOT NULL CHECK (embedding_model <> ''),
            created_at timestamptz NOT NULL,
            UNIQUE (source_item_id, ord),
            FOREIGN KEY (source_item_id, workspace_id)
                REFERENCES source_item (id, workspace_id) ON DELETE CASCADE,
            FOREIGN KEY (connection_id, workspace_id)
                REFERENCES connection (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    for table in ("index_scope", "source_item", "chunk"):
        op.execute(
            f"CREATE INDEX {table}_workspace_connection_idx"
            f" ON {table} (workspace_id, connection_id)"
        )
    op.execute("CREATE INDEX sync_state_workspace_id_idx ON sync_state (workspace_id)")
    op.execute(
        "CREATE INDEX chunk_embedding_hnsw ON chunk USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX chunk_tokens_gin ON chunk USING gin (tokens)")

    expr = "workspace_id = " + _SETTING
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR ALL USING ({expr})"
            f" WITH CHECK ({expr})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO orbit_app")

    # 주기 작업용 조회: 함수 소유자에게만 SELECT를 연다(함수 밖에서는 workspace_isolation만 적용).
    for table in ("connection", "index_scope", "sync_state"):
        op.execute(f"CREATE POLICY due_lookup ON {table} FOR SELECT TO CURRENT_USER USING (true)")
    op.execute(
        """
        CREATE FUNCTION mail_index_due_connections(
            p_stale_before timestamptz, p_error_retry_before timestamptz
        )
        RETURNS TABLE (workspace_id uuid, connection_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT c.workspace_id, c.id
            FROM public.connection AS c
            WHERE c.status = 'active'
              AND EXISTS (
                  SELECT 1 FROM public.index_scope AS s
                  WHERE s.connection_id = c.id AND s.workspace_id = c.workspace_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM public.sync_state AS t
                  WHERE t.connection_id = c.id AND t.workspace_id = c.workspace_id
                    AND (
                        (t.status <> 'error' AND t.last_synced_at IS NOT NULL
                            AND t.last_synced_at >= p_stale_before)
                        OR (t.status = 'error'
                            AND (t.error_kind = 'permission_revoked'
                                 OR t.updated_at >= p_error_retry_before))
                    )
              )
            ORDER BY c.id
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION mail_index_due_connections(timestamptz, timestamptz) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION mail_index_due_connections(timestamptz, timestamptz)"
        " TO orbit_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION mail_index_due_connections(timestamptz, timestamptz)")
    for table in ("connection", "index_scope", "sync_state"):
        op.execute(f"DROP POLICY IF EXISTS due_lookup ON {table}")
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE connection DROP CONSTRAINT connection_id_workspace_key")
