"""SPIKE-003 Procrastinate 앱과 작업. 스파이크 코드이며 제품 코드로 쓰지 않는다."""

import os
import time

import procrastinate
import psycopg

from common import DSN

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=DSN),
    import_paths=["tasks"],
)


def marker(name: str) -> None:
    print(f"MARKER {name}", flush=True)


@app.task(name="execute_run")
def execute_run(run_id: str, sleep_before: float = 0, sleep_after: float = 0) -> None:
    """run_effect에 1건 기록한다. SIGKILL 시점을 잡기 위한 MARKER와 sleep을 둔다.

    환경변수 SPIKE_NO_SLEEP=1 이면 sleep을 건너뛴다(복구용 워커가 60초씩 기다리지 않게 하려는 용도).
    """
    no_sleep = os.environ.get("SPIKE_NO_SLEEP") == "1"
    marker("effect_before")
    if sleep_before and not no_sleep:
        time.sleep(sleep_before)
    with psycopg.connect(DSN) as conn:  # with 블록이 정상 종료되면 커밋한다
        conn.execute(
            "INSERT INTO run_effect (run_id, pid) VALUES (%s, %s)", (run_id, os.getpid())
        )
    marker("effect_committed")
    if sleep_after and not no_sleep:
        time.sleep(sleep_after)
