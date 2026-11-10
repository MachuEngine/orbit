"""SPIKE-002 공통: 접속 정보, 스키마·데이터 준비, 예외 기록 도우미. 스파이크 코드다."""

import os
import random
import uuid

import psycopg
from psycopg import sql
from pgvector import Vector
from pgvector.psycopg import register_vector

PORT = 55433
DB = "orbit_spike"
PW = os.environ.get("ORBIT_SPIKE_PG_PASSWORD", "spike")

NS = uuid.UUID("00000000-0000-0000-0000-000000000002")
WS = {name: str(uuid.uuid5(NS, name)) for name in ("A", "B", "L", "S")}
ROWS = {"A": 40, "B": 40, "L": 5000, "S": 30}
DIM = 8
SEED = 20260101
POLICY = "workspace_isolation"
HNSW_INDEX = "chunk_embedding_hnsw"


def sa_url(user: str) -> str:
    return f"postgresql+psycopg://{user}:{PW}@localhost:{PORT}/{DB}"


def pg_connect(user: str, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host="localhost", port=PORT, dbname=DB, user=user, password=PW, autocommit=autocommit
    )


def rand_vec(rng: random.Random) -> list[float]:
    return [round(rng.uniform(-1.0, 1.0), 6) for _ in range(DIM)]


def vec_text(v: list[float]) -> str:
    return Vector(v).to_text()


POLICY_NULLIF = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)
POLICY_PLAIN = "workspace_id = current_setting('app.workspace_id', true)::uuid"


def create_policy(conn: psycopg.Connection, expr: str, to_public: bool = False) -> None:
    """정책을 만든다. 기본은 `TO orbit_app`, `to_public=True`면 대상 지정 없음(= PUBLIC)."""
    target = sql.SQL("") if to_public else sql.SQL(" TO orbit_app")
    conn.execute(
        sql.SQL("CREATE POLICY {} ON chunk FOR ALL{} USING ({}) WITH CHECK ({})").format(
            sql.Identifier(POLICY), target, sql.SQL(expr), sql.SQL(expr)
        )
    )


def set_policy(expr: str, to_public: bool = False) -> None:
    """orbit_owner로 정책을 교체한다."""
    with pg_connect("orbit_owner") as conn:
        conn.execute(sql.SQL("DROP POLICY IF EXISTS {} ON chunk").format(sql.Identifier(POLICY)))
        create_policy(conn, expr, to_public)


def rls_state() -> dict:
    """정책 정의(Postgres가 정규화한 식)와 RLS·FORCE 플래그를 읽는다(복구 검증용)."""
    with pg_connect("orbit_owner") as conn:
        pols = conn.execute(
            "SELECT policyname, cmd, roles::text, qual, with_check FROM pg_policies"
            " WHERE tablename='chunk' ORDER BY policyname"
        ).fetchall()
        flags = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='chunk'"
        ).fetchone()
    return {"policies": [list(p) for p in pols], "rowsecurity": flags[0], "force": flags[1]}


def set_force(on: bool) -> None:
    with pg_connect("orbit_owner") as conn:
        conn.execute(f"ALTER TABLE chunk {'FORCE' if on else 'NO FORCE'} ROW LEVEL SECURITY")


def setup_database() -> None:
    """역할, 확장, 테이블, 정책, 데이터, HNSW 인덱스를 처음부터 만든다."""
    with pg_connect("postgres") as su:
        for role, extra in (("orbit_owner", "NOSUPERUSER NOBYPASSRLS"), ("orbit_app", "NOSUPERUSER NOBYPASSRLS")):
            exists = su.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            su.execute(
                sql.SQL("{} ROLE {} LOGIN PASSWORD {} " + extra).format(
                    sql.SQL(verb), sql.Identifier(role), sql.Literal(PW)
                )
            )
        su.execute("CREATE EXTENSION IF NOT EXISTS vector")
        su.execute("DROP TABLE IF EXISTS chunk")
        su.execute("GRANT ALL ON SCHEMA public TO orbit_owner")
        su.execute("GRANT USAGE ON SCHEMA public TO orbit_app")

    with pg_connect("orbit_owner") as ow:
        ow.execute(
            "CREATE TABLE chunk (id uuid PRIMARY KEY, workspace_id uuid NOT NULL,"
            " text text, embedding vector(8))"
        )
        ow.execute("ALTER TABLE chunk ENABLE ROW LEVEL SECURITY")
        create_policy(ow, POLICY_NULLIF)
        ow.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON chunk TO orbit_app")

        register_vector(ow)
        rng = random.Random(SEED)
        rows = []
        for name, n in ROWS.items():
            for i in range(n):
                rows.append(
                    (str(uuid.uuid5(NS, f"{name}-{i}")), WS[name], f"synthetic {name} chunk {i}", Vector(rand_vec(rng)))
                )
        with ow.cursor() as cur:
            cur.executemany("INSERT INTO chunk (id, workspace_id, text, embedding) VALUES (%s,%s,%s,%s)", rows)
        ow.execute(f"CREATE INDEX {HNSW_INDEX} ON chunk USING hnsw (embedding vector_l2_ops)")
        ow.execute("ANALYZE chunk")


def owner_counts() -> dict[str, int]:
    """RLS를 우회하는 소유자 연결로 워크스페이스별 행 수를 센다(기준 확인용)."""
    inv = {v: k for k, v in WS.items()}
    with pg_connect("orbit_owner") as ow:
        rows = ow.execute("SELECT workspace_id::text, count(*) FROM chunk GROUP BY 1").fetchall()
    return {inv.get(w, w): c for w, c in rows}


def owner_checksum(ws: str) -> str:
    with pg_connect("orbit_owner") as ow:
        return ow.execute(
            "SELECT md5(string_agg(id::text || text, ',' ORDER BY id)) FROM chunk WHERE workspace_id=%s", (WS[ws],)
        ).fetchone()[0]


def exc_info(e: BaseException) -> dict:
    orig = getattr(e, "orig", None) or e
    first = str(orig).strip().splitlines()[0] if str(orig).strip() else ""
    return {
        "class": f"{type(e).__module__}.{type(e).__qualname__}",
        "orig_class": f"{type(orig).__module__}.{type(orig).__qualname__}",
        "sqlstate": getattr(orig, "sqlstate", None),
        "message": first,
    }
