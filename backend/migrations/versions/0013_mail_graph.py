"""A4 GraphRAG 평가용 메일 그래프 테이블 (ADR-0036, 평가 전용)

Revision ID: 0013
Revises: 0012

- `mail_graph_entity`: 같은 정규화 이름(`name_key`)은 워크스페이스당 한 행이다. `source_item_ids`는
  개체가 나온 메일(`source_item.id`) 목록이다. 배열이라 FK는 없다(평가 색인은 메일을 지우지 않는다).
  `embedding`은 색인 임베더(`chunk`와 같은 모델·차원)가 만든 이름 임베딩이다.
  구축 중에는 NULL일 수 있다.
- `mail_graph_relation`: 메일 한 통이 말하는 삼중항 하나. 객체는 개체(`object_entity_id`)이거나
  메일에 나온 값(`object_value`, 날짜·금액 등)이다. 출처 메일은 `source_item_id` FK다.
- `mail_graph_community`: 커뮤니티 요약과 출처 메일 ID 목록, 요약 임베딩, 요약 비용·토큰.
- `mail_graph_extraction`: 메일별 추출 결과 상태와 비용·토큰(재개와 비용 기록용). 본문은 없다.
- `mail_graph_build`: 워크스페이스당 한 행. 구축이 끝났을 때만 있고 구축 결과 해시·설정·합계가 있다.
- 모든 테이블에 `workspace_id` FK, RLS ENABLE + FORCE, `workspace_isolation` 정책을 건다. 개체·
  커뮤니티 임베딩에는 벡터 인덱스를 두지 않는다(정확 탐색, 평가 코퍼스는 작다).
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_SETTING = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_TABLES = (
    "mail_graph_entity",
    "mail_graph_relation",
    "mail_graph_community",
    "mail_graph_extraction",
    "mail_graph_build",
)
_WS = "workspace_id uuid NOT NULL REFERENCES workspace (id) ON DELETE CASCADE"
_SOURCE_FK = (
    "FOREIGN KEY (source_item_id, workspace_id)"
    " REFERENCES source_item (id, workspace_id) ON DELETE CASCADE"
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE mail_graph_entity (
            id uuid PRIMARY KEY,
            {_WS},
            name_key text NOT NULL CHECK (name_key <> ''),
            name text NOT NULL CHECK (name <> ''),
            kind text NOT NULL CHECK (kind IN
                ('person', 'company', 'project', 'document', 'place', 'decision')),
            source_item_ids uuid[] NOT NULL CHECK (cardinality(source_item_ids) > 0),
            embedding vector(1536),
            embedding_model text,
            created_at timestamptz NOT NULL,
            UNIQUE (workspace_id, name_key),
            UNIQUE (id, workspace_id)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE mail_graph_relation (
            id uuid PRIMARY KEY,
            {_WS},
            source_item_id uuid NOT NULL,
            subject_entity_id uuid NOT NULL,
            predicate text NOT NULL CHECK (predicate <> ''),
            object_entity_id uuid,
            object_value text,
            CHECK ((object_entity_id IS NULL) <> (object_value IS NULL)),
            {_SOURCE_FK},
            FOREIGN KEY (subject_entity_id, workspace_id)
                REFERENCES mail_graph_entity (id, workspace_id) ON DELETE CASCADE,
            FOREIGN KEY (object_entity_id, workspace_id)
                REFERENCES mail_graph_entity (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE mail_graph_community (
            id uuid PRIMARY KEY,
            {_WS},
            community_key text NOT NULL CHECK (community_key <> ''),
            entity_ids uuid[] NOT NULL,
            source_item_ids uuid[] NOT NULL CHECK (cardinality(source_item_ids) > 0),
            summary text NOT NULL CHECK (summary <> ''),
            summary_embedding vector(1536),
            embedding_model text,
            config_tag text NOT NULL,
            input_tokens integer NOT NULL,
            output_tokens integer NOT NULL,
            cost_usd numeric(14, 8),
            latency_ms integer NOT NULL,
            created_at timestamptz NOT NULL,
            UNIQUE (workspace_id, community_key)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE mail_graph_extraction (
            source_item_id uuid PRIMARY KEY,
            {_WS},
            status text NOT NULL CHECK (status IN ('ok', 'format_error')),
            config_tag text NOT NULL,
            entity_count integer NOT NULL,
            relation_count integer NOT NULL,
            dropped_relations integer NOT NULL,
            input_tokens integer NOT NULL,
            output_tokens integer NOT NULL,
            cost_usd numeric(14, 8),
            latency_ms integer NOT NULL,
            created_at timestamptz NOT NULL,
            {_SOURCE_FK}
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mail_graph_build (
            workspace_id uuid PRIMARY KEY REFERENCES workspace (id) ON DELETE CASCADE,
            graph_digest text NOT NULL CHECK (graph_digest <> ''),
            config jsonb NOT NULL,
            stats jsonb NOT NULL,
            built_at timestamptz NOT NULL
        )
        """
    )
    for table, column in (
        ("mail_graph_relation", "subject_entity_id"),
        ("mail_graph_relation", "object_entity_id"),
        ("mail_graph_relation", "source_item_id"),
    ):
        op.execute(f"CREATE INDEX {table}_{column}_idx ON {table} (workspace_id, {column})")
    expr = "workspace_id = " + _SETTING
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR ALL USING ({expr})"
            f" WITH CHECK ({expr})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO orbit_app")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE {table}")
