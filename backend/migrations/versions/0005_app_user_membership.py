"""사용자·멤버십·로그인 함수 (ADR-0016 §1, ADR-0004)

Revision ID: 0005
Revises: 0004

- `app_user`(Google `sub`로 식별하는 로그인 사용자)는 워크스페이스에 속하지 않는다. 그래서
  `orbit_app`에는 테이블 권한을 주지 않고(기본 거부), FORCE RLS를 켠 채 함수 소유자에게만
  적용되는 정책(`login_provision`)을 둔다. 앱은 아래 `SECURITY DEFINER` 함수로만 접근한다.
  함수는 0004의 `auth_session_lookup`과 같은 방식이다: `search_path` 고정, PUBLIC EXECUTE 회수,
  `orbit_app`에만 EXECUTE.
  - `login_upsert_user(...)`: 사용자를 만들거나 갱신하고, 처음이면 개인 워크스페이스와 owner
    멤버십을 한 트랜잭션에서 만든다. ID·시각은 앱이 정해(UUIDv7, 주입 Clock) 넘긴다. 이미 있는
    사용자면 넘긴 ID는 쓰지 않는다. 같은 `sub`의 동시 첫 로그인은 `ON CONFLICT`가 직렬화한다.
  - `auth_session_profile(token_hash)`: 그 토큰의 세션에 묶인 사용자의 표시 이름·이메일만
    돌려준다(본인 정보).
- `membership`은 워크스페이스 범위 테이블이다(`workspace_isolation`). 함수 소유자만 쓸 수 있고,
  `orbit_app`은 현재 워크스페이스의 행을 읽기만 한다.
- `workspace`에 `type`·`owner_user_id`를 더한다. 개발용 워크스페이스는 사용자가 없다.
- `auth_session.user_id`는 NULL을 허용한다(개발용 로그인은 사용자 없음). 값이 있으면
  `(workspace_id, user_id)` 복합 FK로 그 워크스페이스의 멤버만 가리킬 수 있다.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app_user (
            id uuid PRIMARY KEY,
            google_sub text NOT NULL UNIQUE CHECK (google_sub <> ''),
            email text NOT NULL,
            display_name text NOT NULL,
            created_at timestamptz NOT NULL,
            last_login_at timestamptz NOT NULL
        )
        """
    )
    op.execute("ALTER TABLE app_user ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app_user FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY login_provision ON app_user FOR ALL TO CURRENT_USER USING (true)")
    # orbit_app에는 app_user 권한을 주지 않는다. 아래 함수로만 접근한다.

    op.execute(
        "ALTER TABLE workspace ADD COLUMN type text NOT NULL DEFAULT 'personal'"
        " CHECK (type IN ('personal'))"
    )
    op.execute(
        "ALTER TABLE workspace ADD COLUMN owner_user_id uuid REFERENCES app_user (id)"
        " ON DELETE SET NULL"
    )
    # 로그인 함수(소유자 권한)가 워크스페이스를 만들고 읽는다. `workspace_isolation`은 그대로다.
    op.execute("CREATE POLICY login_provision ON workspace FOR ALL TO CURRENT_USER USING (true)")

    op.execute(
        """
        CREATE TABLE membership (
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
            role text NOT NULL CHECK (role IN ('owner', 'member')),
            PRIMARY KEY (workspace_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX membership_user_id_idx ON membership (user_id)")
    expr = "workspace_id = " + _SETTING
    op.execute("ALTER TABLE membership ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE membership FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON membership FOR ALL USING ({expr})"
        f" WITH CHECK ({expr})"
    )
    op.execute("CREATE POLICY login_provision ON membership FOR ALL TO CURRENT_USER USING (true)")
    op.execute("GRANT SELECT ON membership TO orbit_app")

    op.execute("ALTER TABLE auth_session ADD COLUMN user_id uuid")
    op.execute(
        "ALTER TABLE auth_session ADD FOREIGN KEY (workspace_id, user_id)"
        " REFERENCES membership (workspace_id, user_id) ON DELETE CASCADE"
    )

    op.execute(
        """
        CREATE FUNCTION login_upsert_user(
            p_google_sub text,
            p_email text,
            p_display_name text,
            p_user_id uuid,
            p_workspace_id uuid,
            p_now timestamptz
        )
        RETURNS TABLE (user_id uuid, workspace_id uuid, created boolean)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_user_id uuid;
            v_inserted boolean;
            v_workspace_id uuid;
        BEGIN
            INSERT INTO public.app_user AS u
                (id, google_sub, email, display_name, created_at, last_login_at)
            VALUES (p_user_id, p_google_sub, p_email, p_display_name, p_now, p_now)
            ON CONFLICT (google_sub) DO UPDATE
                SET email = EXCLUDED.email,
                    display_name = EXCLUDED.display_name,
                    last_login_at = EXCLUDED.last_login_at
            RETURNING u.id, (xmax = 0) INTO v_user_id, v_inserted;

            IF v_inserted THEN
                v_workspace_id := p_workspace_id;
                INSERT INTO public.workspace (id, created_at, type, owner_user_id)
                VALUES (v_workspace_id, p_now, 'personal', v_user_id);
                INSERT INTO public.membership (workspace_id, user_id, role)
                VALUES (v_workspace_id, v_user_id, 'owner');
            ELSE
                SELECT m.workspace_id INTO v_workspace_id
                FROM public.membership AS m
                JOIN public.workspace AS w ON w.id = m.workspace_id
                WHERE m.user_id = v_user_id AND m.role = 'owner' AND w.type = 'personal'
                ORDER BY w.created_at, w.id
                LIMIT 1;
            END IF;
            RETURN QUERY SELECT v_user_id, v_workspace_id, v_inserted;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION login_upsert_user(text, text, text, uuid, uuid, timestamptz)"
        " FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION login_upsert_user(text, text, text, uuid, uuid, timestamptz)"
        " TO orbit_app"
    )

    op.execute(
        """
        CREATE FUNCTION auth_session_profile(p_token_hash text)
        RETURNS TABLE (user_id uuid, email text, display_name text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT u.id, u.email, u.display_name
            FROM public.auth_session AS s
            JOIN public.app_user AS u ON u.id = s.user_id
            WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION auth_session_profile(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION auth_session_profile(text) TO orbit_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION auth_session_profile(text)")
    op.execute("DROP FUNCTION login_upsert_user(text, text, text, uuid, uuid, timestamptz)")
    op.execute("ALTER TABLE auth_session DROP COLUMN user_id")
    op.execute("DROP TABLE membership")
    op.execute("DROP POLICY login_provision ON workspace")
    op.execute("ALTER TABLE workspace DROP COLUMN owner_user_id")
    op.execute("ALTER TABLE workspace DROP COLUMN type")
    op.execute("DROP TABLE app_user")
