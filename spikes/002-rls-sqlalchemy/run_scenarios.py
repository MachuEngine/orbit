"""SPIKE-002 시나리오 R1~R9 실행기. 스파이크 코드이며 제품 코드로 쓰지 않는다.

워크스페이스 설정은 이벤트 훅 없이 각 시나리오에서 `set_config` 를 명시적으로 호출한다.
"""

import argparse
import asyncio
import hashlib
import json
import random
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import create_async_engine

import common as C
from common import WS, exc_info

HERE = Path(__file__).parent
SET_WS = text("SELECT set_config('app.workspace_id', :ws, :local)")
PID = text("SELECT pg_backend_pid()")
COUNT = text("SELECT count(*) FROM chunk")
CURRENT = text("SELECT current_setting('app.workspace_id', true)")


class RecoveryError(RuntimeError):
    """R7·R8 이 바꾼 FORCE·정책이 원상태로 돌아오지 않았다. 이후 결과를 믿을 수 없으므로 중단한다."""


BASELINE: dict = {}  # main() 이 setup 직후 C.rls_state() 로 채운다


def app_engine(pool_size: int, max_overflow: int = 0, **kw):
    return create_engine(C.sa_url("orbit_app"), pool_size=pool_size, max_overflow=max_overflow,
                         pool_timeout=120, **kw)


def verify_recovery(where: str) -> int:
    """FORCE 해제, 정책 정의 원상태, orbit_app 이 A 를 설정했을 때 40행을 보는지 확인한다."""
    state = C.rls_state()
    problems = []
    if state["force"]:
        problems.append("FORCE 가 해제되지 않았다")
    if state != BASELINE:
        problems.append(f"정책/플래그가 기준과 다르다: {state} != {BASELINE}")
    eng = app_engine(1)
    try:
        with eng.begin() as c:
            set_ws(c, "A")
            rows_a = c.execute(COUNT).scalar_one()
    finally:
        eng.dispose()
    if rows_a != C.ROWS["A"]:
        problems.append(f"A 설정 시 {rows_a}행 (기대 {C.ROWS['A']})")
    if problems:
        raise RecoveryError(f"{where} 복구 검증 실패: " + " / ".join(problems))
    return rows_a


def set_ws(conn, name: str, local: bool = True) -> None:
    conn.execute(SET_WS, {"ws": WS[name], "local": local})


RLS_MSG = "row-level security policy"


def is_rls_denial(err: dict | None) -> bool:
    """RLS 정책 때문에 거부됐을 때만 True. sqlstate 42501 이고 메시지에 정책 문구가 있어야 한다."""
    return bool(err) and err["sqlstate"] == "42501" and RLS_MSG in err["message"]


def rls_control(eng, tag: str) -> dict:
    """대조군: A 를 설정하면 A 행 삽입과 수정이 성공한다. 삽입은 커밋하고, 끝나면 소유자가 지운다."""
    rid = f"00000000-0000-0000-0000-0000000000{tag}"
    d: dict = {"insert_rows": None, "update_rows": None, "error": None}
    try:
        with eng.begin() as c:
            set_ws(c, "A")
            d["insert_rows"] = c.execute(
                text("INSERT INTO chunk (id, workspace_id, text) VALUES (:id, :ws, 'control')"),
                {"id": rid, "ws": WS["A"]}).rowcount
        with eng.begin() as c:
            set_ws(c, "A")
            d["update_rows"] = c.execute(
                text("UPDATE chunk SET text='control-updated' WHERE id=:id"), {"id": rid}).rowcount
    except Exception as e:  # noqa: BLE001
        d["error"] = exc_info(e)
    with C.pg_connect("orbit_owner") as oc:
        d["persisted_before_cleanup"] = oc.execute("SELECT count(*) FROM chunk WHERE id=%s", (rid,)).fetchone()[0]
        oc.execute("DELETE FROM chunk WHERE id=%s", (rid,))
        d["left_after_cleanup"] = oc.execute("SELECT count(*) FROM chunk WHERE id=%s", (rid,)).fetchone()[0]
    d["ok"] = (d["error"] is None and d["insert_rows"] == 1 and d["update_rows"] == 1
               and d["persisted_before_cleanup"] == 1 and d["left_after_cleanup"] == 0)
    return d


def result(sid: str, kind: str, passed, obs: dict, data: dict) -> dict:
    return {"id": sid, "kind": kind, "pass": passed, "obs": obs, "data": data}


# ── R1 ────────────────────────────────────────────────
def r1() -> dict:
    eng = app_engine(1)
    try:
        with eng.begin() as c:
            set_ws(c, "A")
            rows = c.execute(text("SELECT workspace_id::text, count(*) FROM chunk GROUP BY 1")).all()
    finally:
        eng.dispose()
    seen = {w: n for w, n in rows}
    ok = seen == {WS["A"]: C.ROWS["A"]}
    return result("R1", "검증", ok, {"visible": ",".join(f"{'A' if w == WS['A'] else w}:{n}" for w, n in seen.items())},
                  {"seen": seen, "expected": {WS["A"]: C.ROWS["A"]}})


# ── R2 ────────────────────────────────────────────────
def r2() -> dict:
    eng = app_engine(1)
    new_id = "00000000-0000-0000-0000-0000000000a2"
    ins = text("INSERT INTO chunk (id, workspace_id, text) VALUES (:id, :ws, 'r2')")
    try:
        with eng.begin() as c:
            n = c.execute(COUNT).scalar_one()
        err = None
        try:
            with eng.begin() as c:
                c.execute(ins, {"id": new_id, "ws": WS["A"]})
        except Exception as e:  # noqa: BLE001 - 예외 종류를 기록하는 것이 목적
            err = exc_info(e)
    finally:
        eng.dispose()
    with C.pg_connect("orbit_owner") as oc:
        inserted = oc.execute("SELECT count(*) FROM chunk WHERE id=%s", (new_id,)).fetchone()[0]
    eng = app_engine(1)
    try:
        control = rls_control(eng, "c2")
    finally:
        eng.dispose()
    denied = is_rls_denial(err)
    ok = n == 0 and denied and inserted == 0 and control["ok"]
    return result("R2", "검증", ok,
                  {"select_rows": n, "insert": "거부" if err else "허용", "insert_exc": err and err["orig_class"],
                   "insert_sqlstate": err and err["sqlstate"], "insert_msg": err and err["message"],
                   "rls_denial(42501+정책문구)": denied, "persisted": inserted,
                   "대조군(A 삽입·수정 성공)": control["ok"],
                   "판정": "INVALID(대조군 실패)" if not control["ok"] else ("유효" if denied else "거부 원인 불일치")},
                  {"select_rows": n, "insert_error": err, "persisted_rows": inserted, "control": control})


# ── R3 ────────────────────────────────────────────────
def r3() -> dict:
    before = C.owner_checksum("B")
    eng = app_engine(1)
    bid = "00000000-0000-0000-0000-0000000000a3"
    data: dict = {}
    try:
        try:
            with eng.begin() as c:
                set_ws(c, "A")
                c.execute(text("INSERT INTO chunk (id, workspace_id, text) VALUES (:id, :ws, 'r3')"),
                          {"id": bid, "ws": WS["B"]})
            data["insert_B"] = None
        except Exception as e:  # noqa: BLE001
            data["insert_B"] = exc_info(e)
        with eng.begin() as c:
            set_ws(c, "A")
            data["update_B_rows"] = c.execute(
                text("UPDATE chunk SET text='tampered' WHERE workspace_id=:ws"), {"ws": WS["B"]}).rowcount
            data["delete_B_rows"] = c.execute(
                text("DELETE FROM chunk WHERE workspace_id=:ws"), {"ws": WS["B"]}).rowcount
        # 참고: A 행을 B 로 옮기는 UPDATE (WITH CHECK 확인). 거부되면 롤백된다.
        try:
            with eng.begin() as c:
                set_ws(c, "A")
                data["move_A_to_B_rows"] = c.execute(
                    text("UPDATE chunk SET workspace_id=:b WHERE id=:id"),
                    {"b": WS["B"], "id": str(uuid.uuid5(C.NS, "A-0"))}).rowcount
                raise RuntimeError("rollback")  # 허용돼도 반영하지 않는다
        except RuntimeError:
            data["move_A_to_B"] = "허용(롤백함)"
        except Exception as e:  # noqa: BLE001
            data["move_A_to_B"] = exc_info(e)
    finally:
        eng.dispose()
    after = C.owner_checksum("B")
    counts = C.owner_counts()
    data["B_unchanged"] = before == after
    data["B_rows_after"] = counts.get("B")
    eng = app_engine(1)
    try:
        data["control"] = rls_control(eng, "c3")
    finally:
        eng.dispose()
    data["owner_counts_after_control"] = C.owner_counts()
    denied = is_rls_denial(data["insert_B"])
    ok = (denied and data["update_B_rows"] == 0 and data["delete_B_rows"] == 0
          and data["B_unchanged"] and counts.get("B") == C.ROWS["B"] and data["control"]["ok"]
          and data["owner_counts_after_control"] == C.ROWS)
    ib = data["insert_B"]
    mv = data.get("move_A_to_B")
    return result("R3", "검증", ok,
                  {"insert_B": "거부" if data["insert_B"] else "허용",
                   "insert_exc": ib and ib["orig_class"], "insert_sqlstate": ib and ib["sqlstate"],
                   "insert_msg": ib and ib["message"], "rls_denial(42501+정책문구)": denied,
                   "대조군(A 삽입·수정 성공)": data["control"]["ok"],
                   "판정": "INVALID(대조군 실패)" if not data["control"]["ok"] else ("유효" if denied else "거부 원인 불일치"),
                   "update_B_rows": data["update_B_rows"], "delete_B_rows": data["delete_B_rows"],
                   "B_unchanged": data["B_unchanged"],
                   "move_A_to_B": mv if isinstance(mv, str) else f"거부 {mv['orig_class']} {mv['sqlstate']}: {mv['message']}"},
                  data)


# ── R4 / R5 ───────────────────────────────────────────
ENDS = ("commit", "rollback", "exception", "db_error")


def reuse_case(local: bool, end: str, **engine_kw) -> dict:
    """풀 크기 1 에서 요청 1(A 설정)이 `end` 방식으로 끝난 뒤 요청 2(설정 없이 조회)를 실행한다."""
    eng = app_engine(1, **engine_kw)
    d: dict = {"end": end}
    try:
        try:
            if end == "rollback":
                c = eng.connect()
                tx = c.begin()
                d["pid1"] = c.execute(PID).scalar_one()
                set_ws(c, "A", local)
                d["rows1"] = c.execute(COUNT).scalar_one()
                tx.rollback()
                c.close()
            else:
                with eng.begin() as c:
                    d["pid1"] = c.execute(PID).scalar_one()
                    set_ws(c, "A", local)
                    d["rows1"] = c.execute(COUNT).scalar_one()
                    if end == "exception":
                        raise RuntimeError("simulated application error")
                    if end == "db_error":
                        c.execute(text("SELECT 1/0"))
        except Exception as e:  # noqa: BLE001
            d["req1_exc"] = exc_info(e)["orig_class"]
        with eng.connect() as c:
            d["pid2"] = c.execute(PID).scalar_one()
            d["setting2"] = c.execute(CURRENT).scalar_one()
            d["rows2"] = c.execute(COUNT).scalar_one()
    finally:
        eng.dispose()
    d["same_pid"] = d["pid1"] == d["pid2"]
    d["leaked"] = d["rows2"] > 0
    return d


NO_RESET = {"pool_reset_on_return": None}  # 풀의 반환 시 reset(rollback)을 끈 변형 (create_engine 파라미터명)


def raw_open_txn_case(local: bool, **engine_kw) -> dict:
    """트랜잭션을 연 채 풀에 반환하는 경로. Connection 객체를 거치지 않으므로 SQLAlchemy 가 반환 전에 끝내 주지 않는다.

    요청 1: raw_connection() 으로 DBAPI 연결을 얻어 set_config(A) 실행 후 커밋·롤백 없이 close()(= 풀 반환).
    요청 2: 같은 엔진(pool_size=1)에서 설정 없이 조회한다.
    풀 `reset`·`checkin` 이벤트에서 호출 횟수와 그 시점의 libpq 트랜잭션 상태를 기록한다.
    """
    eng = app_engine(1, **engine_kw)
    ev: list[dict] = []

    @event.listens_for(eng, "reset")
    def _on_reset(dbapi_conn, record, reset_state):  # noqa: ANN001
        ev.append({"event": "reset", "txn_status": dbapi_conn.info.transaction_status.name,
                   "transaction_was_reset": reset_state.transaction_was_reset})

    @event.listens_for(eng, "checkin")
    def _on_checkin(dbapi_conn, record):  # noqa: ANN001
        ev.append({"event": "checkin", "txn_status": dbapi_conn.info.transaction_status.name})

    d: dict = {}
    try:
        raw = eng.raw_connection()
        cur = raw.cursor()
        cur.execute("SELECT pg_backend_pid()")
        d["pid1"] = cur.fetchone()[0]
        cur.execute("SELECT set_config('app.workspace_id', %s, %s)", (WS["A"], local))
        cur.execute("SELECT count(*) FROM chunk")
        d["rows1"] = cur.fetchone()[0]
        cur.close()
        raw.close()  # 커밋·롤백 없이 풀에 반환
        raw = eng.raw_connection()
        d["status_at_checkout2"] = raw.driver_connection.info.transaction_status.name
        cur = raw.cursor()
        cur.execute("SELECT pg_backend_pid()")
        d["pid2"] = cur.fetchone()[0]
        cur.execute("SELECT current_setting('app.workspace_id', true)")
        d["setting2"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM chunk")
        d["rows2"] = cur.fetchone()[0]
        cur.close()
        raw.rollback()  # 요청 2 는 정리해서 반환한다(관찰 이후이므로 결과에 영향 없음)
        raw.close()
    finally:
        eng.dispose()
    first = ev[:2]  # 요청 1 반환 시점의 이벤트(reset, checkin)
    d["events_req1"] = first
    d["reset_calls_req1"] = sum(1 for e in first if e["event"] == "reset")
    d["checkin_calls_req1"] = sum(1 for e in first if e["event"] == "checkin")
    d["status_at_reset"] = next((e["txn_status"] for e in first if e["event"] == "reset"), None)
    d["status_at_checkin"] = next((e["txn_status"] for e in first if e["event"] == "checkin"), None)
    d["same_pid"] = d["pid1"] == d["pid2"]
    d["leaked"] = d["rows2"] > 0
    return d


def fmt_raw(c: dict) -> str:
    return (f"reset호출={c['reset_calls_req1']} checkin호출={c['checkin_calls_req1']} "
            f"reset시점상태={c['status_at_reset']} checkin시점상태={c['status_at_checkin']} "
            f"요청2체크아웃상태={c['status_at_checkout2']} rows1={c['rows1']} rows2={c['rows2']} "
            f"setting2={c['setting2']!r} same_pid={c['same_pid']} 누출={'예' if c['leaked'] else '아니오'}")


def r4() -> dict:
    cases = [reuse_case(True, e) for e in ENDS]
    ok = all(c["same_pid"] and c["rows1"] == C.ROWS["A"] and c["rows2"] == 0 for c in cases)  # 판정은 기본 엔진 기준
    variant = [reuse_case(True, e, **NO_RESET) for e in ENDS]
    raw_default = raw_open_txn_case(True)
    raw_no_reset = raw_open_txn_case(True, **NO_RESET)
    obs = {}
    for c in cases:
        obs[f"{c['end']}"] = f"rows1={c['rows1']} rows2={c['rows2']} setting2={c['setting2']!r} same_pid={c['same_pid']}"
    for c in variant:
        obs[f"[reset=None, Connection 경유] {c['end']}"] = (
            f"rows1={c['rows1']} rows2={c['rows2']} setting2={c['setting2']!r} same_pid={c['same_pid']}")
    obs["[관찰] 열린 트랜잭션 반환(raw_connection), 기본 엔진"] = fmt_raw(raw_default)
    obs["[관찰] 열린 트랜잭션 반환(raw_connection), reset=None"] = fmt_raw(raw_no_reset)
    return result("R4", "검증", ok, obs, {"cases": cases, "cases_no_reset": variant,
                                          "raw_open_txn_default": raw_default, "raw_open_txn_no_reset": raw_no_reset})


def r5() -> dict:
    cases = [reuse_case(False, e) for e in ENDS]
    variant = [reuse_case(False, e, **NO_RESET) for e in ENDS]
    raw_default = raw_open_txn_case(False)
    raw_no_reset = raw_open_txn_case(False, **NO_RESET)
    obs = {}
    for tag, cs in (("", cases), ("[reset=None, Connection 경유] ", variant)):
        for c in cs:
            obs[f"{tag}{c['end']}"] = (f"rows1={c['rows1']} rows2={c['rows2']} setting2={c['setting2']!r} "
                                       f"same_pid={c['same_pid']} 누출={'예' if c['leaked'] else '아니오'}")
    obs["열린 트랜잭션 반환(raw_connection), 기본 엔진"] = fmt_raw(raw_default)
    obs["열린 트랜잭션 반환(raw_connection), reset=None"] = fmt_raw(raw_no_reset)
    return result("R5", "관찰", None, obs, {"cases": cases, "cases_no_reset": variant,
                                            "raw_open_txn_default": raw_default, "raw_open_txn_no_reset": raw_no_reset})


# ── R6 ────────────────────────────────────────────────
SYNC_THREADS, SYNC_POOL, SYNC_PER = 16, 4, 40
ASYNC_TASKS, ASYNC_POOL, ASYNC_PER = 50, 4, 10


def tally(rows, name: str | None, t: dict) -> None:
    """name 이 None 이면 워크스페이스를 설정하지 않은 요청이다. 어떤 행이든 보이면 교차 노출이다."""
    t["requests"] += 1
    t["rows_seen"] += len(rows)
    t["cross_rows"] += sum(1 for (w,) in rows if name is None or str(w) != WS[name])
    if len(rows) != (0 if name is None else C.ROWS[name]):
        t["wrong_count_requests"] += 1


def new_tally() -> dict:
    return {"requests": 0, "rows_seen": 0, "cross_rows": 0, "wrong_count_requests": 0, "errors": 0}


def merge(parts: list[dict]) -> dict:
    return {k: sum(p[k] for p in parts) for k in parts[0]}


PID_SEQ = text("SELECT pg_backend_pid()")


def pick(rng: random.Random, skip_ratio: float) -> str | None:
    """A/B 중 하나를 고른다. skip_ratio 만큼은 설정을 건너뛰는 요청(None)이다. 비율 0 이면 기존 하네스와 같다."""
    name = rng.choice("AB")
    if skip_ratio and rng.random() < skip_ratio:
        return None
    return name


def pid_evidence(events: list[tuple[int, str | None]]) -> dict:
    """events 는 연결을 쥔 채 기록한 (pid, 워크스페이스) 순서열이다. pid 별 요청 수와 A↔B 전환 횟수를 센다."""
    seq: dict[int, list[str]] = {}
    for pid, name in events:
        seq.setdefault(pid, []).append(name)
    per_pid = {}
    for pid, names in seq.items():
        ab = [n for n in names if n in ("A", "B")]
        per_pid[pid] = {"requests": len(names), "transitions_AB": sum(1 for x, y in zip(ab, ab[1:]) if x != y)}
    return {"distinct_pids": len(per_pid), "per_pid": per_pid,
            "requests_per_pid": sorted(v["requests"] for v in per_pid.values()),
            "transitions_per_pid": sorted(v["transitions_AB"] for v in per_pid.values()),
            "total_transitions": sum(v["transitions_AB"] for v in per_pid.values())}


def r6_sync(seed: int, local: bool = True, skip_ratio: float = 0.0) -> dict:
    eng = app_engine(SYNC_POOL)
    events: list[tuple[int, str | None]] = []
    lock = threading.Lock()

    def worker(i: int) -> dict:
        rng = random.Random(seed * 1000 + i)
        t = new_tally()
        for _ in range(SYNC_PER):
            name = pick(rng, skip_ratio)
            try:
                with eng.begin() as c:
                    pid = c.execute(PID_SEQ).scalar_one()
                    if name is not None:
                        set_ws(c, name, local)
                    time.sleep(rng.uniform(0, 0.003))
                    rows = c.execute(text("SELECT workspace_id FROM chunk")).all()
                    with lock:  # 연결을 쥔 동안 기록하므로 같은 pid 의 순서가 실제 사용 순서와 같다
                        events.append((pid, name))
                tally(rows, name, t)
            except Exception:  # noqa: BLE001
                t["errors"] += 1
        return t

    try:
        with ThreadPoolExecutor(SYNC_THREADS) as ex:
            out = merge(list(ex.map(worker, range(SYNC_THREADS))))
    finally:
        eng.dispose()
    out.update(threads=SYNC_THREADS, pool_size=SYNC_POOL, evidence=pid_evidence(events))
    return out


async def r6_async(seed: int, local: bool = True, skip_ratio: float = 0.0) -> dict:
    eng = create_async_engine(C.sa_url("orbit_app"), pool_size=ASYNC_POOL, max_overflow=0, pool_timeout=120)
    events: list[tuple[int, str | None]] = []

    async def task(i: int) -> dict:
        rng = random.Random(seed * 1000 + 500 + i)
        t = new_tally()
        for _ in range(ASYNC_PER):
            name = pick(rng, skip_ratio)
            try:
                async with eng.begin() as c:
                    pid = (await c.execute(PID_SEQ)).scalar_one()
                    if name is not None:
                        await c.execute(SET_WS, {"ws": WS[name], "local": local})
                    await asyncio.sleep(rng.uniform(0, 0.003))
                    rows = (await c.execute(text("SELECT workspace_id FROM chunk"))).all()
                    events.append((pid, name))
                tally(rows, name, t)
            except Exception:  # noqa: BLE001
                t["errors"] += 1
        return t

    try:
        out = merge(await asyncio.gather(*(task(i) for i in range(ASYNC_TASKS))))
    finally:
        await eng.dispose()
    out.update(tasks=ASYNC_TASKS, pool_size=ASYNC_POOL, evidence=pid_evidence(events))
    return out


def fmt_r6(x: dict) -> str:
    ev = x["evidence"]
    return (f"requests={x['requests']} cross_rows={x['cross_rows']} wrong_count={x['wrong_count_requests']} "
            f"errors={x['errors']} | pids={ev['distinct_pids']} requests_per_pid={ev['requests_per_pid']} "
            f"AB_transitions_per_pid={ev['transitions_per_pid']} (합 {ev['total_transitions']})")


def r6(seed: int) -> dict:
    s = r6_sync(seed)
    a = asyncio.run(r6_async(seed))
    ok_main = all(x["cross_rows"] == 0 and x["errors"] == 0 and x["wrong_count_requests"] == 0
                  and x["requests"] == x_expected for x, x_expected in
                  ((s, SYNC_THREADS * SYNC_PER), (a, ASYNC_TASKS * ASYNC_PER)))
    obs = {"sync": fmt_r6(s), "async": fmt_r6(a)}
    # 대조군. 기존 하네스는 모든 요청이 읽기 전에 설정하므로 세션 범위여도 노출이 안 생길 수 있다.
    # 그래서 설정을 건너뛰는 요청을 25% 섞은 하네스를 트랜잭션 범위·세션 범위로 각각 한 번 더 돌린다.
    # 시드·요청 수는 고정이지만 스레드 스케줄은 비결정적이므로 민감도는 매 회차 확인한다.
    neg: dict = {}
    for label, local, skip in (("neg_session_same_harness", False, 0.0),
                               ("mix_skip25_txn_scope", True, 0.25),
                               ("neg_mix_skip25_session_scope", False, 0.25)):
        ns = r6_sync(seed, local, skip)
        na = asyncio.run(r6_async(seed, local, skip))
        neg[label] = {"sync": ns, "async": na}
        tag = "[판정 포함]" if label != "neg_session_same_harness" else "[관찰]"
        obs[f"{tag} {label} sync"] = fmt_r6(ns)
        obs[f"{tag} {label} async"] = fmt_r6(na)
    mix = neg["mix_skip25_txn_scope"]
    mix_safe = all(x["cross_rows"] == 0 and x["wrong_count_requests"] == 0 and x["errors"] == 0
                   for x in (mix["sync"], mix["async"]))
    negm = neg["neg_mix_skip25_session_scope"]
    sensitive = all(x["cross_rows"] > 0 for x in (negm["sync"], negm["async"]))  # 세션 범위에서 누출이 검출돼야 하네스가 유효
    ok = ok_main and mix_safe and sensitive
    obs["대조군: 설정 누락 혼합, 트랜잭션 범위 안전"] = mix_safe
    obs["하네스 민감도: 세션 범위 혼합에서 cross_rows>0 (sync·async)"] = sensitive
    obs["판정"] = ("INVALID(하네스 민감도 미증명)" if not sensitive
                 else ("유효" if ok else "FAIL(기본 시나리오 또는 혼합 대조군 위반)"))
    return result("R6", "검증", ok, obs, {"sync": s, "async": a, "seed": seed, "controls": neg,
                                          "mix_txn_scope_safe": mix_safe, "harness_sensitive": sensitive})


# ── R7 ────────────────────────────────────────────────
def owner_count(set_a: bool = False) -> int:
    with C.pg_connect("orbit_owner", autocommit=False) as c:
        if set_a:
            c.execute("SELECT set_config('app.workspace_id', %s, true)", (WS["A"],))
        n = c.execute("SELECT count(*) FROM chunk").fetchone()[0]
        c.commit()
    return n


def r7() -> dict:
    d: dict = {}
    try:
        d["no_force_no_setting"] = owner_count()
        C.set_force(True)
        d["force_no_setting"] = owner_count()
        d["force_set_A"] = owner_count(set_a=True)
    finally:
        C.set_force(False)
    d["restored_no_force"] = owner_count()
    verify_recovery("R7(TO orbit_app)")
    # 변형: 정책 대상을 PUBLIC(대상 지정 없음)으로 바꾸면 FORCE + A 설정 시 소유자가 A 행을 보는가
    try:
        C.set_policy(C.POLICY_NULLIF, to_public=True)
        d["public_no_force_no_setting"] = owner_count()
        d["public_no_force_set_A"] = owner_count(set_a=True)
        C.set_force(True)
        d["public_force_no_setting"] = owner_count()
        d["public_force_set_A"] = owner_count(set_a=True)
    finally:
        C.set_force(False)
        C.set_policy(C.POLICY_NULLIF)
    d["recovered_rows_A"] = verify_recovery("R7(TO PUBLIC 변형)")
    total = sum(C.ROWS.values())
    obs = {k: v for k, v in d.items()}
    obs["total_rows"] = total
    obs["해석"] = ("FORCE 전: 소유자 RLS 우회=" + ("예" if d["no_force_no_setting"] == total else "아니오")
                 + " / FORCE 후 설정 없음: " + str(d["force_no_setting"]) + "행"
                 + " / FORCE+A 설정(TO orbit_app): " + str(d["force_set_A"]) + "행"
                 + " / FORCE+A 설정(TO PUBLIC): " + str(d["public_force_set_A"]) + "행"
                 + ("(소유자가 A 행을 본다)" if d["public_force_set_A"] == C.ROWS["A"] else ""))
    return result("R7", "관찰", None, obs, d)


# ── R8 ────────────────────────────────────────────────
def r8() -> dict:
    d: dict = {}
    try:
        C.set_policy(C.POLICY_PLAIN)
        eng = app_engine(1)
        try:
            with eng.begin() as c:
                d["pid1"] = c.execute(PID).scalar_one()
                set_ws(c, "A")
                d["rows_with_A"] = c.execute(COUNT).scalar_one()
            try:
                with eng.connect() as c:
                    d["pid2"] = c.execute(PID).scalar_one()
                    d["setting2"] = c.execute(CURRENT).scalar_one()
                    d["reused_conn_rows"] = c.execute(COUNT).scalar_one()
                    d["reused_conn_error"] = None
            except Exception as e:  # noqa: BLE001
                d["reused_conn_error"] = exc_info(e)
        finally:
            eng.dispose()
        eng2 = app_engine(1)  # 한 번도 설정한 적 없는 새 연결
        try:
            try:
                with eng2.connect() as c:
                    d["fresh_setting"] = c.execute(CURRENT).scalar_one()
                    d["fresh_conn_rows"] = c.execute(COUNT).scalar_one()
                    d["fresh_conn_error"] = None
            except Exception as e:  # noqa: BLE001
                d["fresh_conn_error"] = exc_info(e)
        finally:
            eng2.dispose()
    finally:
        C.set_policy(C.POLICY_NULLIF)
    d["restored_rows_A"] = verify_recovery("R8")  # 불일치하면 RecoveryError

    def fmt(prefix: str) -> str:
        e = d.get(f"{prefix}_error")
        if e:
            return f"오류 {e['orig_class']} sqlstate={e['sqlstate']}: {e['message']}"
        return f"{d.get(f'{prefix}_rows')}행"

    obs = {"재사용 연결(설정 후 해제)": fmt("reused_conn"), "새 연결(설정 이력 없음)": fmt("fresh_conn"),
           "정책 복구 후 A": d["restored_rows_A"]}
    return result("R8", "관찰", None, obs, d)


# ── R9 ────────────────────────────────────────────────
ITER_MODES = ("off", "relaxed_order", "strict_order")
PLANS = {"natural": {}, "seqscan_off": {"enable_seqscan": "off"}}
KNN = text("SELECT id::text, workspace_id::text FROM chunk ORDER BY embedding <-> CAST(:q AS vector) LIMIT 10")
EXPLAIN_JSON = text("EXPLAIN (ANALYZE, FORMAT JSON) SELECT id::text, workspace_id::text FROM chunk "
                    "ORDER BY embedding <-> CAST(:q AS vector) LIMIT 10")


def exact_top10(ws: str, q: str) -> list[str]:
    with C.pg_connect("orbit_owner", autocommit=False) as c:
        c.execute("SELECT set_config('enable_indexscan', 'off', true)")
        rows = c.execute(
            "SELECT id::text FROM chunk WHERE workspace_id=%s ORDER BY embedding <-> %s::vector LIMIT 10",
            (WS[ws], q)).fetchall()
        c.commit()
    return [r[0] for r in rows]


def plan_nodes(node: dict):
    yield node
    for ch in node.get("Plans", []):
        yield from plan_nodes(ch)


def index_topk_distribution(q: str) -> dict:
    """소유자 연결(RLS 우회)에서 HNSW 인덱스를 강제로 타고 상위 ef_search 개의 워크스페이스 분포를 센다."""
    inv = {v: k for k, v in WS.items()}
    with C.pg_connect("orbit_owner", autocommit=False) as c:
        force, owner_ok = c.execute(
            "SELECT relforcerowsecurity, pg_get_userbyid(relowner) = current_user FROM pg_class WHERE relname='chunk'"
        ).fetchone()
        if force or not owner_ok:
            raise RecoveryError(f"R9 소유자 측정 전제 위반: relforcerowsecurity={force}, 연결 역할이 소유자={owner_ok}")
        for k, v in (("hnsw.iterative_scan", "off"), ("enable_seqscan", "off"), ("enable_bitmapscan", "off")):
            c.execute("SELECT set_config(%s, %s, true)", (k, v))
        # hnsw.* 설정값은 pgvector 인덱스를 한 번 써야 등록되므로 먼저 가벼운 인덱스 스캔을 실행한다
        c.execute("SELECT 1 FROM chunk ORDER BY embedding <-> %s::vector LIMIT 1", (q,)).fetchall()
        ef = int(c.execute("SELECT current_setting('hnsw.ef_search')").fetchone()[0])
        sqltext = (f"SELECT workspace_id::text FROM chunk ORDER BY embedding <-> %s::vector LIMIT {ef}")
        plan = c.execute("EXPLAIN (FORMAT JSON) " + sqltext, (q,)).fetchone()[0]
        plan = json.loads(plan) if isinstance(plan, str) else plan
        uses_index = any(n.get("Index Name") == C.HNSW_INDEX for n in plan_nodes(plan[0]["Plan"]))
        rows = c.execute(sqltext, (q,)).fetchall()
        c.commit()
    dist = Counter(inv.get(r[0], r[0]) for r in rows)
    return {"ef_search": ef, "returned": len(rows), "returned_equals_ef_search": len(rows) == ef,
            "force_rls": force, "role_is_owner": owner_ok, "uses_hnsw_index": uses_index,
            "by_workspace": dict(dist), "S_rows": dist.get("S", 0)}


def r9() -> dict:
    q = C.vec_text(C.rand_vec(random.Random(C.SEED + 999)))
    qhash = hashlib.md5(q.encode()).hexdigest()[:8]
    # 서버 측 준비문장(prepared statement)이 계획에 끼어드는 것을 막는다: prepare_threshold=None
    eng = app_engine(1, connect_args={"prepare_threshold": None})
    variants: dict = {}
    obs: dict = {}
    topk = index_topk_distribution(q)
    obs["hnsw.ef_search"] = topk["ef_search"]
    obs["소유자 인덱스 스캔 상위 ef_search 분포"] = (
        f"returned={topk['returned']}(==ef_search: {topk['returned_equals_ef_search']}) "
        f"force_rls={topk['force_rls']} owner={topk['role_is_owner']} index={topk['uses_hnsw_index']} by_workspace={topk['by_workspace']} "
        f"S_rows={topk['S_rows']}")
    try:
        with eng.connect() as c:
            obs["prepare_threshold"] = repr(c.connection.driver_connection.prepare_threshold)
        for ws in ("S", "L"):  # L 은 큰 워크스페이스 대조군
            truth = exact_top10(ws, q)
            for mode in ITER_MODES:
                for pname, gucs in PLANS.items():
                    with eng.begin() as c:
                        set_ws(c, ws)
                        c.execute(text("SELECT set_config('hnsw.iterative_scan', :v, true)"), {"v": mode})
                        for k, v in gucs.items():
                            c.execute(text("SELECT set_config(:k, :v, true)"), {"k": k, "v": v})
                        raw = c.execute(EXPLAIN_JSON, {"q": q}).scalar_one()
                        raw = json.loads(raw) if isinstance(raw, str) else raw
                        top = raw[0]["Plan"]
                        nodes = list(plan_nodes(top))
                        idx_nodes = [n for n in nodes if n.get("Index Name") == C.HNSW_INDEX]
                        rows = c.execute(KNN, {"q": q}).all()
                        eff = c.execute(text("SELECT current_setting('hnsw.iterative_scan', true)")).scalar_one()
                    ids = [r[0] for r in rows]
                    key = f"{ws}/{mode}/{pname}"
                    v = {"returned": len(rows), "all_in_ws": all(r[1] == WS[ws] for r in rows),
                         "cross_rows": sum(1 for r in rows if r[1] != WS[ws]),
                         "uses_hnsw_index": bool(idx_nodes),
                         "analyze_node_types": [n["Node Type"] for n in nodes],
                         "analyze_index_actual_rows": [n.get("Actual Rows") for n in idx_nodes],
                         "analyze_index_rows_removed_by_filter": [n.get("Rows Removed by Filter") for n in idx_nodes],
                         "analyze_top_actual_rows": top.get("Actual Rows"),
                         "effective_iterative_scan": eff, "query_vector_md5": qhash,
                         "recall_vs_exact": len(set(ids) & set(truth)) / 10, "plan": raw[0]}
                    variants[key] = v
                    obs[key] = (f"returned={v['returned']} in_ws={v['all_in_ws']} index={v['uses_hnsw_index']} "
                                f"index_actual_rows={v['analyze_index_actual_rows']} "
                                f"removed_by_filter={v['analyze_index_rows_removed_by_filter']} "
                                f"recall={v['recall_vs_exact']:.1f} guc={eff}")
    finally:
        eng.dispose()
    # 대조: S/off/seqscan_off 의 인덱스 노드 Rows Removed by Filter 가 소유자 측정의 상위 ef_search 중 비-S 행 수와 같은가
    sv = variants.get("S/off/seqscan_off")
    non_s = topk["ef_search"] - topk["S_rows"]
    removed = sv["analyze_index_rows_removed_by_filter"] if sv else None
    actual = sv["analyze_index_actual_rows"] if sv else None
    match = bool(removed) and removed[0] == non_s
    obs["대조 S/off/seqscan_off"] = (f"Rows Removed by Filter={removed} vs 소유자 상위 {topk['ef_search']} 중 비-S={non_s} "
                                    f"(S={topk['S_rows']}, index Actual Rows={actual}) 일치={match}")
    same = {v["query_vector_md5"] for v in variants.values()} == {qhash}
    obs["모든 변형이 같은 쿼리 벡터"] = f"{same} (md5={qhash}, 변형 {len(variants)}개)"
    return result("R9", "관찰", None, obs, {"variants": variants, "index_topk": topk, "removed_filter_matches_owner_non_S": match,
                                          "same_query_vector": same,
                                          "query_vector_md5": qhash})


# ── 실행·출력 ─────────────────────────────────────────
VERDICT = {True: "PASS", False: "FAIL", None: "-"}


def print_round(results: list[dict]) -> None:
    print("\nID  구분  판정")
    for r in results:
        print(f"{r['id']}  {r['kind']}  {VERDICT[r['pass']]}")
        for k, v in r["obs"].items():
            print(f"      {k}: {v}")
        if r["id"] in ("R2", "R3"):
            err = r["data"].get("insert_error") or r["data"].get("insert_B")
            if err:
                print(f"      삽입 예외: {err['class']} / {err['orig_class']} sqlstate={err['sqlstate']}: {err['message']}")
            mv = r["data"].get("move_A_to_B")
            if isinstance(mv, dict):
                print(f"      이동 예외: {mv['orig_class']}: {mv['message']}")


def fmt(c: Counter) -> str:
    return "{" + "; ".join(f"{k}: {n}회" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))) + "}"


def summarize(rounds: list[list[dict]]) -> None:
    n = len(rounds)
    print(f"\n=== 분포 요약 ({n}회) ===")
    for sid in [r["id"] for r in rounds[0]]:
        rs = [next(r for r in rd if r["id"] == sid) for rd in rounds]
        head = sid
        if rs[0]["pass"] is not None:
            head += f" PASS {sum(1 for r in rs if r['pass'])}/{n}"
        print(head)
        for key in rs[0]["obs"]:
            print(f"   {key}: {fmt(Counter(str(r['obs'][key]) for r in rs))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    C.setup_database()
    base = C.owner_counts()
    expected = {k: v for k, v in C.ROWS.items()}
    print("초기 데이터(소유자 기준):", base, "기대:", expected)
    if base != expected:
        print("초기 데이터가 기대와 다르다. 중단한다.")
        return 2
    BASELINE.update(C.rls_state())
    rounds = []
    for i in range(args.repeat):
        print(f"\n##### 회차 {i + 1}/{args.repeat} #####")
        try:
            results = [r1(), r2(), r3(), r4(), r5(), r6(C.SEED + i), r7(), r8(), r9()]
        except RecoveryError as e:
            print(f"\n치명적 오류: {e}\n중단한다.")
            return 1
        after = C.owner_counts()
        if after != expected:
            print("경고: 회차 후 데이터가 변했다", after)
            for r in results:
                if r["id"] in ("R2", "R3"):
                    r["pass"] = False
        print_round(results)
        rounds.append(results)
    summarize(rounds)
    (HERE / "results.json").write_text(json.dumps(
        [{"round": i + 1, "results": rd} for i, rd in enumerate(rounds)], ensure_ascii=False, indent=2, default=str))
    failed = [f"{i + 1}회차 {r['id']}" for i, rd in enumerate(rounds) for r in rd if r["pass"] is False]
    print("\n검증 실패:", failed if failed else "없음")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
