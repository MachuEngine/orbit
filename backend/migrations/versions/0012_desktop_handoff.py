"""데스크톱 로그인 핸드오프 코드와 계정 연결 티켓 (ADR-0024 §4·§5·§7)

Revision ID: 0012
Revises: 0011

- `desktop_login_code`: 시스템 브라우저에서 끝낸 Google 로그인을 앱 창으로 넘기는 일회용 코드.
  코드 원문은 저장하지 않고 SHA-256 해시, PKCE `code_challenge`, 사용자·워크스페이스 ID, 만료·사용
  시각, 교환으로 만든 세션 ID만 둔다(ADR-0006: ID·해시만).
- `desktop_connect_ticket`: 앱 창에서 시스템 브라우저로 계정 연결(동의)을 넘기는 일회용 티켓.
  해시, 의도(`connect`/`docs_scope`), 대상 연결 ID, 사용자·워크스페이스 ID, 앱이 만든 PKCE
  `code_challenge`·loopback 포트·앱 `state`, 만료·사용 시각만 둔다.
- `desktop_connect_pending`: 브라우저에서 동의를 마친 연결의 대기 행(연결 CSRF 방지, ADR-0024 §5).
  콜백은 토큰을 교환·저장하지 않고 Google 인가 코드(와 이 흐름의 PKCE 검증기·nonce)를 봉투
  암호화(`ciphertext`·`nonce`·`wrapped_dek`·`kek_id`, AAD는 행 ID와 워크스페이스)해 이 행에 둔다.
  완료 코드 해시, 앱의 `code_challenge`, 의도, 대상 연결, 만료(5분)·사용 시각이 함께 있다. 앱이 자기
  세션으로 완료 코드를 제출할 때만 쓰인다. 완료 단계는 세션이 있어 RLS 안에서
  처리하므로(`orbit_app`은
  SELECT·INSERT·`used_at` UPDATE) 읽기용 함수는 없고, 만료 행 정리만 `desktop_handoff_purge`가 한다.
- 두 테이블은 `auth_session`(0004)과 같은 방식이다. `workspace_id`·RLS·FORCE RLS·
  `workspace_isolation` 정책이 있고, `orbit_app`은 INSERT만 가진다(로그인 콜백·티켓 발급은
  워크스페이스를
  알고 있어서 `workspace_session`으로 넣는다). 코드·티켓을 받는 시점에는 세션이 없어
  워크스페이스를 모르므로,
  해시로 찾는 `SECURITY DEFINER` 함수가 함수 소유자에게만 적용되는 정책(`handoff_definer`)을 통해
  읽고 갱신한다. 함수는 `search_path`를 고정하고 PUBLIC EXECUTE를 회수한 뒤 `orbit_app`에만 연다.
- `desktop_login_code_redeem`: 해시·`code_challenge`·만료·미사용을 한 `UPDATE ... RETURNING`으로
  검사하고 사용 처리한다(동시에 두 번 와도 한 쪽만 성공). 결과는 `ok` / `reused`(이미 사용한 코드를
  올바른 challenge로 다시 보냄, 앞서 만든 세션 ID를 돌려줘 폐기하게 한다) / `invalid`(그 밖의 모든
  경우, 사유를 구분하지 않는다).
- `desktop_connect_ticket_redeem`: 해시·만료·미사용을 같은 방식으로 검사하고 사용 처리한다.
- `desktop_handoff_purge`: 만료 시각이 기준보다 오래된 행을 세 테이블에서 지운다(주기 작업).
- `session_id`에는 FK를 두지 않는다. 세션 행이 먼저 없어도 되고, 세션이 지워져도 기록은 남는다.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_HASH = "'^[0-9a-f]{64}$'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE desktop_login_code (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            user_id uuid NOT NULL,
            code_hash text NOT NULL UNIQUE CHECK (code_hash ~ {_HASH}),
            code_challenge text NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{{43}}$'),
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            session_id uuid,
            FOREIGN KEY (workspace_id, user_id)
                REFERENCES membership (workspace_id, user_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX desktop_login_code_workspace_id_idx ON desktop_login_code (workspace_id)"
    )
    op.execute("CREATE INDEX desktop_login_code_expires_idx ON desktop_login_code (expires_at)")
    op.execute(
        f"""
        CREATE TABLE desktop_connect_ticket (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            user_id uuid NOT NULL,
            ticket_hash text NOT NULL UNIQUE CHECK (ticket_hash ~ {_HASH}),
            intent text NOT NULL CHECK (intent IN ('connect', 'docs_scope')),
            connection_id uuid,
            code_challenge text NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{{43}}$'),
            port integer NOT NULL CHECK (port BETWEEN 1024 AND 65535),
            app_state text NOT NULL CHECK (app_state ~ '^[A-Za-z0-9_-]{{16,128}}$'),
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            CHECK (
                (intent = 'connect' AND connection_id IS NULL)
                OR (intent = 'docs_scope' AND connection_id IS NOT NULL)
            ),
            FOREIGN KEY (workspace_id, user_id)
                REFERENCES membership (workspace_id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (connection_id, workspace_id)
                REFERENCES connection (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX desktop_connect_ticket_workspace_id_idx"
        " ON desktop_connect_ticket (workspace_id)"
    )
    op.execute(
        "CREATE INDEX desktop_connect_ticket_expires_idx ON desktop_connect_ticket (expires_at)"
    )

    op.execute(
        f"""
        CREATE TABLE desktop_connect_pending (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            user_id uuid NOT NULL,
            code_hash text NOT NULL UNIQUE CHECK (code_hash ~ {_HASH}),
            code_challenge text NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{{43}}$'),
            intent text NOT NULL CHECK (intent IN ('connect', 'docs_scope')),
            connection_id uuid,
            ciphertext bytea NOT NULL,
            nonce bytea NOT NULL,
            wrapped_dek bytea NOT NULL,
            kek_id text NOT NULL,
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            CHECK (
                (intent = 'connect' AND connection_id IS NULL)
                OR (intent = 'docs_scope' AND connection_id IS NOT NULL)
            ),
            FOREIGN KEY (workspace_id, user_id)
                REFERENCES membership (workspace_id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (connection_id, workspace_id)
                REFERENCES connection (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX desktop_connect_pending_workspace_id_idx"
        " ON desktop_connect_pending (workspace_id)"
    )
    op.execute(
        "CREATE INDEX desktop_connect_pending_expires_idx ON desktop_connect_pending (expires_at)"
    )

    expr = "workspace_id = " + _SETTING
    for table in ("desktop_login_code", "desktop_connect_ticket", "desktop_connect_pending"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR ALL USING ({expr})"
            f" WITH CHECK ({expr})"
        )
        # 함수 소유자(SECURITY DEFINER 실행 중의 current_user)에게만 적용되는 정책
        op.execute(f"CREATE POLICY handoff_definer ON {table} FOR ALL TO CURRENT_USER USING (true)")
        if table == "desktop_connect_pending":
            op.execute(f"GRANT SELECT, INSERT, UPDATE (used_at) ON {table} TO orbit_app")
        else:
            op.execute(f"GRANT INSERT ON {table} TO orbit_app")

    op.execute(
        """
        CREATE FUNCTION desktop_login_code_redeem(
            p_code_hash text,
            p_challenge text,
            p_now timestamptz,
            p_session_id uuid
        )
        RETURNS TABLE (
            o_outcome text,
            o_user_id uuid,
            o_workspace_id uuid,
            o_revoke_session_id uuid
        )
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_user uuid;
            v_ws uuid;
            v_session uuid;
        BEGIN
            UPDATE public.desktop_login_code AS c
               SET used_at = p_now, session_id = p_session_id
             WHERE c.code_hash = p_code_hash AND c.used_at IS NULL
               AND c.expires_at > p_now AND c.code_challenge = p_challenge
            RETURNING c.user_id, c.workspace_id INTO v_user, v_ws;
            IF FOUND THEN
                RETURN QUERY SELECT 'ok'::text, v_user, v_ws, NULL::uuid;
                RETURN;
            END IF;
            SELECT c.user_id, c.workspace_id, c.session_id INTO v_user, v_ws, v_session
              FROM public.desktop_login_code AS c
             WHERE c.code_hash = p_code_hash AND c.used_at IS NOT NULL
               AND c.code_challenge = p_challenge AND c.session_id IS NOT NULL;
            IF FOUND THEN
                RETURN QUERY SELECT 'reused'::text, v_user, v_ws, v_session;
                RETURN;
            END IF;
            RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::uuid, NULL::uuid;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION desktop_login_code_redeem(text, text, timestamptz, uuid)"
        " FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION desktop_login_code_redeem(text, text, timestamptz, uuid)"
        " TO orbit_app"
    )

    op.execute(
        """
        CREATE FUNCTION desktop_connect_ticket_redeem(p_ticket_hash text, p_now timestamptz)
        RETURNS TABLE (
            o_user_id uuid,
            o_workspace_id uuid,
            o_intent text,
            o_connection_id uuid,
            o_code_challenge text,
            o_port integer,
            o_app_state text
        )
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            UPDATE public.desktop_connect_ticket AS t
               SET used_at = p_now
             WHERE t.ticket_hash = p_ticket_hash AND t.used_at IS NULL AND t.expires_at > p_now
            RETURNING t.user_id, t.workspace_id, t.intent, t.connection_id, t.code_challenge,
            t.port,
                      t.app_state
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION desktop_connect_ticket_redeem(text, timestamptz) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION desktop_connect_ticket_redeem(text, timestamptz) TO orbit_app"
    )

    op.execute(
        """
        CREATE FUNCTION desktop_handoff_purge(p_before timestamptz)
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_codes integer;
            v_tickets integer;
            v_pending integer;
        BEGIN
            DELETE FROM public.desktop_login_code WHERE expires_at < p_before;
            GET DIAGNOSTICS v_codes = ROW_COUNT;
            DELETE FROM public.desktop_connect_ticket WHERE expires_at < p_before;
            GET DIAGNOSTICS v_tickets = ROW_COUNT;
            DELETE FROM public.desktop_connect_pending WHERE expires_at < p_before;
            GET DIAGNOSTICS v_pending = ROW_COUNT;
            RETURN v_codes + v_tickets + v_pending;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION desktop_handoff_purge(timestamptz) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION desktop_handoff_purge(timestamptz) TO orbit_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION desktop_handoff_purge(timestamptz)")
    op.execute("DROP FUNCTION desktop_connect_ticket_redeem(text, timestamptz)")
    op.execute("DROP FUNCTION desktop_login_code_redeem(text, text, timestamptz, uuid)")
    op.execute("DROP TABLE desktop_connect_pending")
    op.execute("DROP TABLE desktop_connect_ticket")
    op.execute("DROP TABLE desktop_login_code")
