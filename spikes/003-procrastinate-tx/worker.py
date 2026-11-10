"""SPIKE-003 워커 프로세스. `app.run_worker`를 별도 프로세스로 실행한다.

--no-wait: 큐가 비면 종료한다. 기본(wait)은 SIGTERM/SIGINT 로 종료한다.
"""

import argparse
import logging
import os
import sys

from tasks import app


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", required=True)
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--heartbeat-interval", type=float, default=10.0)
    p.add_argument("--stalled-timeout", type=float, default=30.0)
    p.add_argument("--poll-interval", type=float, default=None, help="fetch_job_polling_interval(초). 생략하면 라이브러리 기본값")
    p.add_argument("--no-listen-notify", action="store_true", help="LISTEN/NOTIFY 를 끄고 폴링만으로 작업을 가져온다")
    p.add_argument("--no-sleep", action="store_true", help="execute_run 의 sleep 을 건너뛴다")
    a = p.parse_args()
    if a.no_sleep:
        os.environ["SPIKE_NO_SLEEP"] = "1"
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="LOG %(levelname)s %(message)s")
    extra = {}
    if a.poll_interval is not None:
        extra["fetch_job_polling_interval"] = a.poll_interval
    if a.no_listen_notify:
        extra["listen_notify"] = False
    print(f"MARKER worker_starting pid={os.getpid()} options={extra}", flush=True)
    app.run_worker(
        **extra,
        queues=[a.queue],
        wait=not a.no_wait,
        update_heartbeat_interval=a.heartbeat_interval,
        stalled_worker_timeout=a.stalled_timeout,
    )
    print("MARKER worker_exited", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
