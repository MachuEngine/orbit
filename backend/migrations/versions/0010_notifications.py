"""예약 알림 테이블 2개와 일정 알림 대상 선별 함수 (ADR-0021 §3·§4)

Revision ID: 0010
Revises: 0009

- `notification`: 알림 하나가 한 행이다. `event_reminder`는 연결·캘린더·일정 ID·일정 시작 시각을
  함께 두고 `(workspace_id, connection_id, calendar_id, event_id)`로 유일하다(복합 FK로 `connection`
  삭제 시 CASCADE). `reminder`(대화 리마인더)는 연결과 무관하고 만든 `run_id`를 둔다. run을 지우면
  `run_id`만 비우고 알림은 남긴다. "도착"은 저장하지 않는다
  (`status = 'scheduled' AND due_at <= now`).
- `notification_setting`: 워크스페이스당 한 행(일정 알림 사용 여부, 알림 시점). 행이 없으면 기본값
  (켬, 10분 전)으로 본다.
- 두 테이블 모두 RLS ENABLE + FORCE, `workspace_isolation` 정책. `orbit_app`은 알림 테이블에
  SELECT·INSERT·UPDATE·DELETE, 설정 테이블에 SELECT·INSERT·UPDATE 권한을 갖는다.
- 주기 작업은 워크스페이스를 모르는 상태에서 대상을 골라야 한다. `event_reminder_due_connections`
  (`SECURITY DEFINER`)가 일정 알림을 켠(설정 행이 없으면 켠 것으로 본다) 워크스페이스의 활성 연결
  `(workspace_id, connection_id)`만 돌려준다. 함수 소유자에게만 적용되는 `due_lookup` SELECT 정책이
  `notification_setting`에 있다(`connection`의 정책은 0008에서 만들었다). 제목·주소는 돌려주지
  않는다.
- 보존 기간이 지난 알림의 정리도 워크스페이스를 모르는 주기 작업이 한다.
  `notification_expired_workspaces`(`SECURITY DEFINER`)가 만료된 알림이 있는 `workspace_id`만
  돌려준다(`notification`에도 `due_lookup` 정책).
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE notification (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
            kind text NOT NULL CHECK (kind IN ('event_reminder', 'reminder')),
            title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
            due_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('scheduled', 'cancelled')),
            read_at timestamptz,
            created_at timestamptz NOT NULL,
            connection_id uuid,
            calendar_id text,
            event_id text,
            event_start timestamptz,
            run_id uuid,
            CHECK (
                (kind = 'event_reminder' AND connection_id IS NOT NULL
                    AND calendar_id IS NOT NULL AND event_id IS NOT NULL
                    AND event_start IS NOT NULL)
                OR (kind = 'reminder' AND connection_id IS NULL AND calendar_id IS NULL
                    AND event_id IS NULL AND event_start IS NULL)
            ),
            UNIQUE (workspace_id, connection_id, calendar_id, event_id),
            FOREIGN KEY (connection_id, workspace_id)
                REFERENCES connection (id, workspace_id) ON DELETE CASCADE,
            FOREIGN KEY (run_id, workspace_id)
                REFERENCES run (id, workspace_id) ON DELETE SET NULL (run_id)
        )
        """
    )
    op.execute("CREATE INDEX notification_workspace_due_idx ON notification (workspace_id, due_at)")
    op.execute(
        """
        CREATE TABLE notification_setting (
            workspace_id uuid PRIMARY KEY REFERENCES workspace (id) ON DELETE CASCADE,
            event_reminders_enabled boolean NOT NULL DEFAULT true,
            lead_minutes integer NOT NULL DEFAULT 10 CHECK (lead_minutes IN (5, 10, 15, 30, 60)),
            updated_at timestamptz NOT NULL
        )
        """
    )

    expr = "workspace_id = " + _SETTING
    for table, grants in (
        ("notification", "SELECT, INSERT, UPDATE, DELETE"),
        ("notification_setting", "SELECT, INSERT, UPDATE"),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR ALL USING ({expr})"
            f" WITH CHECK ({expr})"
        )
        op.execute(f"GRANT {grants} ON {table} TO orbit_app")

    # 주기 작업용 조회: 함수 소유자에게만 SELECT를 연다.
    # `connection`의 due_lookup 정책은 0008에 있다.
    op.execute(
        "CREATE POLICY due_lookup ON notification_setting FOR SELECT TO CURRENT_USER USING (true)"
    )
    op.execute(
        """
        CREATE FUNCTION event_reminder_due_connections()
        RETURNS TABLE (workspace_id uuid, connection_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT c.workspace_id, c.id
            FROM public.connection AS c
            LEFT JOIN public.notification_setting AS n ON n.workspace_id = c.workspace_id
            WHERE c.status = 'active'
              AND COALESCE(n.event_reminders_enabled, true)
            ORDER BY c.id
        $$
        """
    )
    # 보존 기간이 지난 알림이 있는 워크스페이스 선별(연결이 없거나 일정 알림이 꺼진 곳도 포함).
    op.execute("CREATE POLICY due_lookup ON notification FOR SELECT TO CURRENT_USER USING (true)")
    op.execute(
        """
        CREATE FUNCTION notification_expired_workspaces(
            p_event_before timestamptz, p_reminder_before timestamptz
        )
        RETURNS TABLE (workspace_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT DISTINCT n.workspace_id
            FROM public.notification AS n
            WHERE (n.kind = 'event_reminder' AND n.event_start < p_event_before)
               OR (n.kind = 'reminder' AND n.due_at < p_reminder_before)
            ORDER BY n.workspace_id
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION notification_expired_workspaces(timestamptz, timestamptz)"
        " FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION notification_expired_workspaces(timestamptz, timestamptz)"
        " TO orbit_app"
    )
    op.execute("REVOKE ALL ON FUNCTION event_reminder_due_connections() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION event_reminder_due_connections() TO orbit_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION notification_expired_workspaces(timestamptz, timestamptz)")
    op.execute("DROP POLICY IF EXISTS due_lookup ON notification")
    op.execute("DROP FUNCTION event_reminder_due_connections()")
    op.execute("DROP POLICY IF EXISTS due_lookup ON notification_setting")
    op.execute("DROP TABLE notification")
    op.execute("DROP TABLE notification_setting")
