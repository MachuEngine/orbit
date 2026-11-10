"""SPIKE-003 공통: DSN, 스키마 적용. 스파이크 코드이며 제품 코드로 쓰지 않는다."""

import os
import time

import psycopg

PASSWORD = os.environ.get("ORBIT_SPIKE_PG_PASSWORD", "spike")
HOST_PORT = 55434
DSN = f"postgresql://orbit_spike:{PASSWORD}@localhost:{HOST_PORT}/orbit_spike"
SA_URL = f"postgresql+psycopg://orbit_spike:{PASSWORD}@localhost:{HOST_PORT}/orbit_spike"

DOMAIN_DDL = """
CREATE TABLE IF NOT EXISTS run (
    id uuid PRIMARY KEY,
    status text NOT NULL
);
CREATE TABLE IF NOT EXISTS run_effect (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL,
    pid integer NOT NULL,
    at timestamptz NOT NULL DEFAULT now()
);
"""


def connect(**kw) -> psycopg.Connection:
    return psycopg.connect(DSN, autocommit=True, **kw)


def ensure_schema(timeout: float = 30) -> None:
    """Procrastinate 스키마(API: SchemaManager.apply_schema)와 도메인 테이블을 만든다. 이미 있으면 건너뛴다."""
    from tasks import app  # 순환 import 방지

    deadline = time.time() + timeout
    while True:
        try:
            with connect() as c:
                has = c.execute("SELECT to_regclass('procrastinate_jobs')").fetchone()[0]
                break
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(0.5)
    if has is None:
        with app.open():
            app.schema_manager.apply_schema()
    with connect() as c:
        c.execute(DOMAIN_DDL)
