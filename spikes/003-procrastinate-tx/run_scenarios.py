"""SPIKE-003 시나리오 Q1~Q7 실행기. 스파이크 코드이며 제품 코드로 쓰지 않는다.

워커는 항상 별도 프로세스(worker.py)로 띄운다. 도메인 쪽 등록 경로는 이 프로세스에서 실행한다.

raw 연결 추출 API (설치된 sqlalchemy 2.1.8 소스 기준)
- 동기: session.connection().connection.driver_connection  -> psycopg.Connection
- 비동기: (await (await session.connection()).get_raw_connection()).driver_connection -> psycopg.AsyncConnection
"""

import argparse
import asyncio
import atexit
import json
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from procrastinate import exceptions as pexc
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from common import SA_URL, connect, ensure_schema
from tasks import app, execute_run

HERE = Path(__file__).parent
ENGINE = create_engine(SA_URL)
INSERT_RUN = text("INSERT INTO run (id, status) VALUES (:id, 'queued')")

KILL_SLEEP = 60  # SIGKILL 시점을 잡기 위해 task 안에서 기다리는 시간(초). 실제로는 그 전에 죽인다.


# ── 프로세스 관리 ─────────────────────────────────────
ALL_PROCS: list["Proc"] = []


class Proc:
    """worker.py 프로세스. stdout/stderr 를 스레드로 계속 읽어 둔다."""

    def __init__(self, queue: str, *flags: str):
        self.p = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "worker.py"), "--queue", queue, *flags],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=HERE,
        )
        self.lines: list[str] = []
        self.cond = threading.Condition()
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()
        ALL_PROCS.append(self)

    def _read(self) -> None:
        for line in self.p.stdout:
            with self.cond:
                self.lines.append(line.rstrip("\n"))
                self.cond.notify_all()

    def wait_line(self, line: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        with self.cond:
            while line not in self.lines:
                left = deadline - time.time()
                if left <= 0 or (self.p.poll() is not None and line not in self.lines and not self.t.is_alive()):
                    return line in self.lines
                self.cond.wait(min(left, 0.2))
        return True

    def markers(self) -> list[str]:
        return [x for x in self.lines if x.startswith("MARKER ")]

    def kill9(self) -> int:
        self.p.send_signal(signal.SIGKILL)
        return self.p.wait()

    def stop(self, timeout: float = 20) -> int:
        """SIGTERM(정상 종료 요청). 시간 안에 안 끝나면 SIGKILL."""
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
        try:
            return self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return self.kill9()

    def wait_exit(self, timeout: float = 60) -> int:
        try:
            return self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return self.kill9()


def cleanup_procs() -> None:
    for pr in ALL_PROCS:
        if pr.p.poll() is None:
            pr.p.kill()
            pr.p.wait()


atexit.register(cleanup_procs)


def drain_worker(queue: str, *flags: str, timeout: float = 60) -> dict:
    """no-wait 워커를 띄워 큐가 빌 때까지 처리하게 한다."""
    pr = Proc(queue, "--no-wait", *flags)
    code = pr.wait_exit(timeout)
    pr.t.join(timeout=3)
    return {"exit": code, "markers": pr.markers()}


# ── DB 조회 ───────────────────────────────────────────
def jobs_of(run_id: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT id, status::text, worker_id, attempts FROM procrastinate_jobs WHERE args->>'run_id' = %s ORDER BY id",
            (run_id,),
        ).fetchall()
    return [{"id": i, "status": s, "worker_id": w, "attempts": a} for i, s, w, a in rows]


def run_count(run_id: str) -> int:
    with connect() as c:
        return c.execute("SELECT count(*) FROM run WHERE id = %s", (run_id,)).fetchone()[0]


def effect_count(run_id: str) -> int:
    with connect() as c:
        return c.execute("SELECT count(*) FROM run_effect WHERE run_id = %s", (run_id,)).fetchone()[0]


def worker_ids() -> set[int]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT id FROM procrastinate_workers").fetchall()}


def job_status(run_id: str) -> str | None:
    js = jobs_of(run_id)
    return js[0]["status"] if js else None


def wait_status(run_id: str, want: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job_status(run_id) == want:
            return True
        time.sleep(0.2)
    return job_status(run_id) == want


# ── 등록 경로 (동기 / 비동기) ──────────────────────────
PROBE = "SELECT pg_backend_pid(), txid_current()"


def probe_sync(s: Session, raw) -> dict:
    """SQLAlchemy 세션 쪽과 raw 연결 쪽에서 같은 쿼리를 실행해 백엔드 pid·txid 를 비교한다."""
    sa_pid, sa_tx = s.execute(text(PROBE)).one()
    raw_pid, raw_tx = raw.execute(PROBE).fetchone()
    return {"sa_pid": sa_pid, "sa_txid": sa_tx, "raw_pid": raw_pid, "raw_txid": raw_tx,
            "same": sa_pid == raw_pid and sa_tx == raw_tx}


async def probe_async(s: AsyncSession, raw) -> dict:
    sa_pid, sa_tx = (await s.execute(text(PROBE))).one()
    raw_pid, raw_tx = await (await raw.execute(PROBE)).fetchone()
    return {"sa_pid": sa_pid, "sa_txid": sa_tx, "raw_pid": raw_pid, "raw_txid": raw_tx,
            "same": sa_pid == raw_pid and sa_tx == raw_tx}


def sync_defer(run_id: str, queue: str, action: str, **cfg) -> dict:
    """동기 SQLAlchemy 트랜잭션에서 run 삽입 + 작업 등록. action: commit | rollback | raise."""
    out = {"job_id": None, "exception": None}
    kwargs = {k: v for k, v in cfg.items() if k in ("sleep_before", "sleep_after")}
    opts = {k: v for k, v in cfg.items() if k not in ("sleep_before", "sleep_after")}
    with Session(ENGINE) as s:
        try:
            if action == "raise":
                with s.begin():  # 예외가 나가면 자동 롤백
                    s.execute(INSERT_RUN, {"id": run_id})
                    raw = s.connection().connection.driver_connection
                    out["job_id"] = execute_run.configure(connection=raw, queue=queue, **opts).defer(
                        run_id=run_id, **kwargs
                    )
                    raise RuntimeError("의도한 예외(커밋 전)")
            else:
                s.execute(INSERT_RUN, {"id": run_id})
                raw = s.connection().connection.driver_connection
                out["raw_type"] = type(raw).__name__
                out["probe_before"] = probe_sync(s, raw)
                out["job_id"] = execute_run.configure(connection=raw, queue=queue, **opts).defer(
                    run_id=run_id, **kwargs
                )
                out["probe_after"] = probe_sync(s, raw)
                s.commit() if action == "commit" else s.rollback()
        except RuntimeError as e:
            out["exception"] = type(e).__name__
    return out


async def _async_defer(run_id: str, queue: str, action: str) -> dict:
    out = {"job_id": None}
    engine = create_async_engine(SA_URL)
    try:
        async with AsyncSession(engine) as s:
            await s.execute(INSERT_RUN, {"id": run_id})
            conn = await s.connection()
            raw = (await conn.get_raw_connection()).driver_connection
            out["raw_type"] = type(raw).__name__
            out["probe_before"] = await probe_async(s, raw)
            out["job_id"] = await execute_run.configure(connection=raw, queue=queue).defer_async(run_id=run_id)
            out["probe_after"] = await probe_async(s, raw)
            await (s.commit() if action == "commit" else s.rollback())
    finally:
        await engine.dispose()
    return out


def async_defer(run_id: str, queue: str, action: str) -> dict:
    return asyncio.run(_async_defer(run_id, queue, action))


def new_ids() -> tuple[str, str]:
    return str(uuid.uuid4()), f"q-{uuid.uuid4().hex[:12]}"


# ── 시나리오 ─────────────────────────────────────────
def commit_flow(defer_fn) -> dict:
    """커밋 후 워커 처리까지의 관측. Q1·Q4 공통."""
    rid, q = new_ids()
    d = defer_fn(rid, q, "commit")
    before = {"run": run_count(rid), "jobs": len(jobs_of(rid)), "status": job_status(rid)}
    w = drain_worker(q)
    after = {"status": job_status(rid), "effects": effect_count(rid)}
    pb, pa = d.get("probe_before"), d.get("probe_after")
    tx_same = bool(pb and pa and pb["same"] and pa["same"])
    ok = (
        tx_same
        and before == {"run": 1, "jobs": 1, "status": "todo"}
        and w["exit"] == 0
        and after == {"status": "succeeded", "effects": 1}
    )
    return {"ok": ok, "raw_type": d.get("raw_type"), "run_rows": before["run"], "jobs": before["jobs"],
            "status_before_worker": before["status"], "worker_exit": w["exit"],
            "status_after_worker": after["status"], "run_effects": after["effects"],
            "tx_same_before_and_after_defer": tx_same,
            "tx_before_defer": pb, "tx_after_defer": pa}


def rollback_flow(defer_fn, action: str) -> dict:
    """롤백/예외 후 run 과 작업이 모두 없는지. Q2·Q3·Q4 공통."""
    rid, q = new_ids()
    d = defer_fn(rid, q, action)
    runs, jobs = run_count(rid), len(jobs_of(rid))
    w = drain_worker(q)
    eff = effect_count(rid)
    ok = runs == 0 and jobs == 0 and eff == 0 and w["exit"] == 0
    return {"ok": ok, "job_id_returned": d["job_id"] is not None, "exception": d.get("exception"),
            "run_rows": runs, "jobs": jobs, "run_effects": eff}


def q1():
    r = commit_flow(sync_defer)
    return {"id": "Q1", "kind": "검증", "pass": r.pop("ok"), "obs": r}


def q2():
    r = rollback_flow(sync_defer, "rollback")
    return {"id": "Q2", "kind": "검증", "pass": r.pop("ok"), "obs": r}


def q3():
    r = rollback_flow(sync_defer, "raise")
    return {"id": "Q3", "kind": "검증", "pass": r.pop("ok"), "obs": r}


def q4():
    c = commit_flow(async_defer)
    rb = rollback_flow(async_defer, "rollback")
    obs = {f"commit_{k}": v for k, v in c.items() if k != "ok"}
    obs.update({f"rollback_{k}": v for k, v in rb.items() if k != "ok"})
    return {"id": "Q4", "kind": "검증", "pass": c["ok"] and rb["ok"], "obs": obs}


Q5_POLL = 0.5  # 워커 fetch_job_polling_interval(초). 라이브러리 기본값은 5.0
Q5_HOLD = 3.0  # 커밋 전 대기(초). 폴링 간격의 6배


def q5():
    """커밋 전 작업을 워커가 가져가지 않는가.

    워커가 대기 구간에 실제로 폴링했다는 증거로 양성 대조군을 쓴다: 대기 시작 직후 같은 큐에 이미 커밋된
    다른 작업을 등록한다. 워커는 LISTEN/NOTIFY 를 끄고(폴링만 가능) 돌므로, 대조군이 대기 중 처리되면 폴링이 일어난 것이다.
    """
    rid, q = new_ids()
    ctl_rid = str(uuid.uuid4())
    before_ids = worker_ids()
    w = Proc(q, "--heartbeat-interval", "1", "--poll-interval", str(Q5_POLL), "--no-listen-notify")
    deadline = time.time() + 20
    while time.time() < deadline and not (worker_ids() - before_ids):
        time.sleep(0.2)
    worker_up = bool(worker_ids() - before_ids)
    time.sleep(1.0)  # 첫 폴링이 끝나도록 잠깐 둔다

    obs: dict = {"worker_registered": worker_up, "poll_interval_s": Q5_POLL, "hold_s": Q5_HOLD,
                 "hold_over_poll_ratio": Q5_HOLD / Q5_POLL,
                 "worker_start_line": next((x for x in w.lines if x.startswith("MARKER worker_starting")), None)}
    with Session(ENGINE) as s:
        s.execute(INSERT_RUN, {"id": rid})
        raw = s.connection().connection.driver_connection
        execute_run.configure(connection=raw, queue=q).defer(run_id=rid)
        obs["own_conn_sees_job"] = s.execute(
            text("SELECT count(*) FROM procrastinate_jobs WHERE args->>'run_id' = :r"), {"r": rid}
        ).scalar()
        obs["other_conn_jobs_right_after_defer"] = len(jobs_of(rid))
        obs["other_conn_run_rows"] = run_count(rid)
        # 양성 대조군: 다른 트랜잭션에서 같은 큐에 커밋된 작업 1건 (대기 시작 직후)
        sync_defer(ctl_rid, q, "commit")
        obs["control_registered_committed"] = len(jobs_of(ctl_rid)) == 1
        time.sleep(Q5_HOLD)  # 커밋 전 대기: 워커는 0.5초마다 폴링한다
        obs["control_status_during_hold"] = job_status(ctl_rid)
        obs["control_effects_during_hold"] = effect_count(ctl_rid)
        obs["other_conn_jobs_after_hold"] = len(jobs_of(rid))
        obs["effects_during_hold"] = effect_count(rid)
        obs["worker_alive_during_hold"] = w.p.poll() is None
        obs["effect_before_markers_during_hold"] = w.markers().count("MARKER effect_before")  # 대조군 1건이면 1
        obs["worker_took_uncommitted_job"] = obs["effects_during_hold"] > 0 or obs["effect_before_markers_during_hold"] != 1
        s.commit()
    done = wait_status(rid, "succeeded", 20)
    obs["status_after_commit"] = job_status(rid)
    obs["effects_after_commit"] = effect_count(rid)
    obs["worker_exit"] = w.stop()
    ok = (
        worker_up
        and obs["own_conn_sees_job"] == 1
        and obs["other_conn_jobs_right_after_defer"] == 0
        and obs["control_registered_committed"]
        and obs["control_status_during_hold"] == "succeeded"  # 폴링이 실제로 일어났다
        and obs["control_effects_during_hold"] == 1
        and obs["other_conn_jobs_after_hold"] == 0
        and obs["effects_during_hold"] == 0
        and obs["worker_alive_during_hold"]
        and not obs["worker_took_uncommitted_job"]
        and done
        and obs["effects_after_commit"] == 1
    )
    return {"id": "Q5", "kind": "검증", "pass": ok, "obs": obs}


async def _stalled(queue: str, seconds_since_heartbeat: float, retry: bool) -> dict:
    async with app.open_async():
        jobs = list(await app.job_manager.get_stalled_jobs(queue=queue, seconds_since_heartbeat=seconds_since_heartbeat))
        if retry:
            for j in jobs:
                await app.job_manager.retry_job(j)
    return {"ids": [j.id for j in jobs]}


def stalled(queue: str, seconds_since_heartbeat: float = 3, retry: bool = False) -> dict:
    return asyncio.run(_stalled(queue, seconds_since_heartbeat, retry))


def q6(case: str):
    """case: before(run_effect 기록 전 SIGKILL) | after(기록·커밋 후 SIGKILL)."""
    rid, q = new_ids()
    kill_marker = "MARKER effect_before" if case == "before" else "MARKER effect_committed"
    sleeps = {"sleep_before": KILL_SLEEP} if case == "before" else {"sleep_after": KILL_SLEEP}
    sync_defer(rid, q, "commit", **sleeps)
    job = jobs_of(rid)[0]

    a = Proc(q, "--heartbeat-interval", "1", "--stalled-timeout", "30")
    seen = a.wait_line(kill_marker, 40)
    code = a.kill9()
    obs: dict = {"marker_seen": seen, "kill_exit": code}
    j = jobs_of(rid)[0]
    obs.update({"status_after_kill": j["status"], "job_worker_id_set": j["worker_id"] is not None,
                "effects_after_kill": effect_count(rid),
                "dead_worker_row_remains": j["worker_id"] in worker_ids() if j["worker_id"] else None})
    valid = seen and code == -9 and j["worker_id"] is not None and obs["effects_after_kill"] == (0 if case == "before" else 1)

    # 1) 죽은 직후 새 워커: 다시 가져가는가
    b = drain_worker(q, "--no-sleep", "--stalled-timeout", "30")
    obs.update({"status_after_new_worker": job_status(rid), "effects_after_new_worker": effect_count(rid),
                "new_worker_exit": b["exit"]})

    # 2) heartbeat 가 멈춘 뒤 멈춘 작업 조회(get_stalled_jobs)
    time.sleep(4.5)
    s1 = stalled(q, 3)
    obs["stalled_found_by_heartbeat"] = job["id"] in s1["ids"]

    # 3) 새 워커가 시작하면서 오래된 워커를 정리(prune_stalled_workers)한다. 작업은 doing 으로 남는지 본다.
    c = drain_worker(q, "--no-sleep", "--stalled-timeout", "3")
    j2 = jobs_of(rid)[0]
    obs.update({"status_after_prune_worker": j2["status"], "job_worker_id_after_prune": j2["worker_id"],
                "dead_worker_row_after_prune": (j["worker_id"] in worker_ids()) if j["worker_id"] else None,
                "effects_after_prune_worker": effect_count(rid)})

    # 4) get_stalled_jobs + retry_job 으로 되돌리고 새 워커로 처리
    s2 = stalled(q, 3, retry=True)
    obs["stalled_found_after_prune"] = job["id"] in s2["ids"]
    obs["status_after_retry_job"] = job_status(rid)
    d = drain_worker(q, "--no-sleep", "--stalled-timeout", "30")
    jf = jobs_of(rid)[0]
    obs.update({"status_final": jf["status"], "attempts_final": jf["attempts"], "run_effects_final": effect_count(rid),
                "recovery_worker_exit": d["exit"]})
    note = None if valid else "INVALID: 전제(marker 관측, kill exit=-9, worker_id 기록, 기록 전/후 effect 수) 불충족"
    return {"id": f"Q6-{case}", "kind": "관찰", "pass": None, "valid": valid, "note": note, "obs": obs}


def q7():
    obs: dict = {}
    lock = f"ql-{uuid.uuid4().hex[:10]}"
    q = f"q-{uuid.uuid4().hex[:12]}"
    r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
    first = sync_defer(r1, q, "commit", queueing_lock=lock)
    obs["first_commit_job"] = first["job_id"] is not None

    def state_probe(s: Session) -> str:
        try:
            s.execute(text("SELECT 1"))
            return "usable"
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}/{type(getattr(e, 'orig', None)).__name__}"

    # 7a: 다른 트랜잭션에서 두 번째 등록 (첫 작업은 커밋되어 todo)
    with Session(ENGINE) as s:
        s.execute(INSERT_RUN, {"id": r2})
        raw = s.connection().connection.driver_connection
        try:
            execute_run.configure(connection=raw, queue=q, queueing_lock=lock).defer(run_id=r2)
            obs["a_other_tx_exception"] = None
        except pexc.AlreadyEnqueued:
            obs["a_other_tx_exception"] = "AlreadyEnqueued"
        except Exception as e:  # noqa: BLE001
            obs["a_other_tx_exception"] = type(e).__name__
        obs["a_tx_state_after"] = state_probe(s)
        s.rollback()
    obs["a_run_rows_after_rollback"] = run_count(r2)

    # 7b: 같은 트랜잭션에서 같은 lock 으로 두 번 등록
    lock_b, r3 = f"ql-{uuid.uuid4().hex[:10]}", str(uuid.uuid4())
    with Session(ENGINE) as s:
        s.execute(INSERT_RUN, {"id": r3})
        raw = s.connection().connection.driver_connection
        execute_run.configure(connection=raw, queue=q, queueing_lock=lock_b).defer(run_id=r3)
        try:
            execute_run.configure(connection=raw, queue=q, queueing_lock=lock_b).defer(run_id=r3)
            obs["b_same_tx_exception"] = None
        except pexc.AlreadyEnqueued:
            obs["b_same_tx_exception"] = "AlreadyEnqueued"
        except Exception as e:  # noqa: BLE001
            obs["b_same_tx_exception"] = type(e).__name__
        obs["b_tx_state_after"] = state_probe(s)
        s.rollback()
    obs["b_run_rows_after_rollback"] = run_count(r3)
    obs["b_jobs_after_rollback"] = len(jobs_of(r3))

    # 7c: SAVEPOINT(begin_nested)로 감싸 두 번째 등록만 되돌리고 바깥 트랜잭션은 커밋
    r4 = str(uuid.uuid4())
    with Session(ENGINE) as s:
        s.execute(INSERT_RUN, {"id": r4})
        raw = s.connection().connection.driver_connection
        try:
            with s.begin_nested():
                # SAVEPOINT 은 SQLAlchemy 가 연결을 쓸 때 늦게 발행된다. raw 연결만 쓰면 발행되지 않으므로
                # 블록 안에서 s.connection() 을 다시 불러 SAVEPOINT 를 먼저 만든다.
                s.connection()
                execute_run.configure(connection=raw, queue=q, queueing_lock=lock).defer(run_id=r4)
            obs["c_savepoint_exception"] = None
        except pexc.AlreadyEnqueued:
            obs["c_savepoint_exception"] = "AlreadyEnqueued"
        except Exception as e:  # noqa: BLE001
            obs["c_savepoint_exception"] = type(e).__name__
        obs["c_tx_state_after"] = state_probe(s)
        try:
            s.commit()
            obs["c_outer_commit"] = "ok"
        except Exception as e:  # noqa: BLE001
            obs["c_outer_commit"] = type(e).__name__
    obs["c_run_rows"] = run_count(r4)
    obs["c_jobs_for_run"] = len(jobs_of(r4))

    # 7d/7e: 첫 등록이 미커밋인 동안 다른 연결이 같은 lock 으로 등록 -> 대기하는가, 첫 트랜잭션 결과에 따라 어떻게 되는가
    for tag, finish in (("d_commit", "commit"), ("e_rollback", "rollback")):
        lk, ra, rb = f"ql-{uuid.uuid4().hex[:10]}", str(uuid.uuid4()), str(uuid.uuid4())
        res: dict = {}
        about_to_defer = threading.Event()

        def second() -> None:
            with Session(ENGINE) as s2:
                s2.execute(INSERT_RUN, {"id": rb})
                raw2 = s2.connection().connection.driver_connection
                try:
                    about_to_defer.set()  # defer 호출 직전
                    res["t_call"] = time.monotonic()
                    execute_run.configure(connection=raw2, queue=q, queueing_lock=lk).defer(run_id=rb)
                    res["exception"] = None
                except pexc.AlreadyEnqueued:
                    res["exception"] = "AlreadyEnqueued"
                except Exception as e:  # noqa: BLE001
                    res["exception"] = type(e).__name__
                res["t_return"] = time.monotonic()
                s2.commit() if res["exception"] is None else s2.rollback()

        with Session(ENGINE) as s1:
            s1.execute(INSERT_RUN, {"id": ra})
            raw1 = s1.connection().connection.driver_connection
            execute_run.configure(connection=raw1, queue=q, queueing_lock=lk).defer(run_id=ra)
            th = threading.Thread(target=second)
            th.start()
            event_ok = about_to_defer.wait(timeout=10)
            t_event = time.monotonic()
            time.sleep(2.0)
            obs[f"{tag}_second_blocked_while_first_open"] = th.is_alive()
            t_end_call = time.monotonic()  # 첫 트랜잭션 종료 호출 직전
            s1.commit() if finish == "commit" else s1.rollback()
            t_end_done = time.monotonic()
            th.join(timeout=15)
        t_ret = res.get("t_return")
        obs[f"{tag}_event_set"] = event_ok
        obs[f"{tag}_first_end_call_s"] = round(t_end_call - t_event, 3)  # 기준: Event 수신 시각
        obs[f"{tag}_first_end_done_s"] = round(t_end_done - t_event, 3)
        obs[f"{tag}_second_return_s"] = round(t_ret - t_event, 3) if t_ret else None
        obs[f"{tag}_second_returned_after_first_end"] = bool(t_ret and t_ret >= t_end_call)
        obs[f"{tag}_second_exception"] = res.get("exception", "TIMEOUT")
        obs[f"{tag}_second_run_rows"] = run_count(rb)
        obs[f"{tag}_second_jobs"] = len(jobs_of(rb))
    return {"id": "Q7", "kind": "관찰", "pass": None, "obs": obs}


# ── 출력 ─────────────────────────────────────────────
def verdict(r: dict) -> str:
    if r.get("valid") is False:
        return "INVALID"
    return {True: "PASS", False: "FAIL", None: "-"}[r["pass"]]


def print_round(results: list[dict]) -> None:
    print("\nID        구분  판정")
    for r in results:
        print(f"{r['id']:<9} {r['kind']}  {verdict(r)}")
        for k, v in r["obs"].items():
            print(f"      {k} = {v}")
        if r.get("note"):
            print(f"      note: {r['note']}")


def hashable(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else v


def fmt(c: Counter) -> str:
    return "{" + ", ".join(f"{k}: {n}회" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))) + "}"


def summarize(rounds: list[list[dict]]) -> None:
    n = len(rounds)
    print(f"\n=== 분포 요약 ({n}회) ===")
    for idx, r0 in enumerate(rounds[0]):
        rs = [rd[idx] for rd in rounds]
        head = r0["id"]
        if r0["pass"] is not None:
            head += f"  PASS {sum(1 for r in rs if r['pass'])}/{n}"
        if "valid" in r0:
            head += f"  유효 {sum(1 for r in rs if r['valid'])}/{n}"
        print(head)
        for k in r0["obs"]:
            print(f"   {k}: {fmt(Counter(hashable(r['obs'].get(k)) for r in rs))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    ensure_schema()
    rounds = []
    try:
        for i in range(args.repeat):
            print(f"\n##### 회차 {i + 1}/{args.repeat} #####", flush=True)
            results = [q1(), q2(), q3(), q4(), q5(), q6("before"), q6("after"), q7()]
            print_round(results)
            rounds.append(results)
    finally:
        cleanup_procs()
    summarize(rounds)
    (HERE / "results.json").write_text(json.dumps(
        [{"round": i + 1, "results": rd} for i, rd in enumerate(rounds)], ensure_ascii=False, indent=2, default=str))
    failed = [f"{i + 1}회차 {r['id']}" for i, rd in enumerate(rounds) for r in rd if r["pass"] is False]
    print("\n검증 실패:", failed if failed else "없음")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
