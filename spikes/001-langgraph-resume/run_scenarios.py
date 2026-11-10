"""SPIKE-001 시나리오 S1~S7 실행기. 각 단계는 별도 프로세스로 `worker.py`를 실행한다."""

import argparse
import json
import signal
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path

import psycopg

from graph import DSN, ensure_effects_table

HERE = Path(__file__).parent


def spawn(*args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", str(HERE / "worker.py"), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=HERE,
    )


def parse(out: str) -> dict:
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    return {"error": "RESULT 줄 없음", "raw_tail": out[-500:]}


def run(*args: str) -> dict:
    p = spawn(*args)
    out, _ = p.communicate()
    return {"exit": p.returncode, "result": parse(out)}


def kill_after_marker(marker: str, *args: str) -> dict:
    """MARKER 줄이 나오면 즉시 SIGKILL 한다."""
    p = spawn(*args)
    seen = False
    for line in p.stdout:
        if line.strip() == f"MARKER {marker}":
            seen = True
            p.send_signal(signal.SIGKILL)
            break
    p.wait()
    return {"exit": p.returncode, "marker_seen": seen}


def counts(thread_id: str) -> dict:
    with psycopg.connect(DSN, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT kind, count(*) FROM spike_effects WHERE thread_id=%s GROUP BY kind", (thread_id,)
        ).fetchall()
    d = {k: int(c) for k, c in rows}
    return {"external_write": d.get("external_write", 0), "pre_interrupt": d.get("pre_interrupt", 0)}


def effect_rows(thread_id: str) -> list[dict]:
    with psycopg.connect(DSN, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT kind, pid, at FROM spike_effects WHERE thread_id=%s ORDER BY id", (thread_id,)
        ).fetchall()
    return [{"kind": k, "pid": pid, "at": at.isoformat()} for k, pid, at in rows]


def is_error(r: dict) -> bool:
    return r["exit"] != 0 or "error" in r["result"]


def pend(r: dict):
    res = r["result"]
    return res.get("pending")


def has_request(pending) -> bool:
    return bool(pending) and all(k in pending[0] for k in ("tool", "args", "args_hash"))


def tid() -> str:
    return str(uuid.uuid4())


def final_values(r: dict):
    return r["result"].get("values") if isinstance(r["result"], dict) else None


def s1():
    t = tid()
    a = run("start", t)
    b = run("pending", t)
    c = run("resume", t, "--approve")
    n = counts(t)
    ok = (
        a["exit"] == 0 and has_request(pend(a)) and has_request(pend(b))
        and c["exit"] == 0 and (final_values(c) or {}).get("result") == "executed"
        and n["external_write"] == 1
    )
    return {"id": "S1", "kind": "검증", "thread_id": t, "pass": ok, "counts": n,
            "procs": {"A_start": a, "B_pending": b, "C_resume": c}}


def s2():
    t = tid()
    a = run("start", t)
    b = run("pending", t)
    c = run("resume", t, "--reject")
    n = counts(t)
    ok = (
        a["exit"] == 0 and has_request(pend(b)) and c["exit"] == 0
        and (final_values(c) or {}).get("result") == "rejected"
        and (final_values(c) or {}).get("decision") == "rejected"
        and n["external_write"] == 0
    )
    return {"id": "S2", "kind": "검증", "thread_id": t, "pass": ok, "counts": n,
            "procs": {"A_start": a, "B_pending": b, "C_resume": c}}


def s3():
    t = tid()
    k = kill_after_marker("plan_started", "start", t, "--plan-seconds", "60")
    cont = run("continue", t)
    pend_p = run("pending", t)
    c = run("resume", t, "--approve")
    n = counts(t)
    ok = (
        k["marker_seen"] and k["exit"] != 0 and cont["exit"] == 0 and has_request(pend(cont))
        and has_request(pend(pend_p)) and c["exit"] == 0
        and (final_values(c) or {}).get("result") == "executed" and n["external_write"] == 1
    )
    return {"id": "S3", "kind": "검증", "thread_id": t, "pass": ok, "counts": n,
            "procs": {"killed_start": k, "continue": cont, "pending": pend_p, "resume": c}}


def s4():
    t = tid()
    start = run("start", t)
    k = kill_after_marker("external_write_committed", "resume", t, "--approve", "--execute-seconds", "60")
    valid = (
        start["exit"] == 0 and has_request(pend(start))
        and k["marker_seen"] is True and k["exit"] == -9
    )
    cont = run("continue", t)
    st = run("status", t)
    n = counts(t)
    return {"id": "S4", "kind": "관찰", "thread_id": t, "pass": None, "counts": n, "valid": valid,
            "procs": {"start": start, "killed_resume": k, "continue": cont, "status": st},
            "note": None if valid else "INVALID: 전제(start 성공+interrupt 도달, marker_seen, kill exit=-9) 불충족. 관측값은 해석 대상에서 제외"}


def s5(s1_result: dict | None = None):
    t = tid()
    run("start", t)
    first = run("resume", t, "--approve")
    n1 = counts(t)
    second = run("resume", t, "--approve")
    n2 = counts(t)
    ew_up = n2["external_write"] > n1["external_write"]
    pre_up = n2["pre_interrupt"] > n1["pre_interrupt"]
    if ew_up:
        outcome = "re-executed"
    elif is_error(second):
        outcome = "error"
    elif pre_up:
        outcome = "re-executed"
    else:
        outcome = "ignored"
    # 기대: 두 번째 재개 뒤 외부 쓰기가 늘지 않는다. 오류로 끝나도 늘지 않으면 PASS.
    ok = first["exit"] == 0 and n1["external_write"] == 1 and not ew_up
    return {"id": "S5", "kind": "검증", "thread_id": t, "pass": ok, "counts": n2, "outcome": outcome,
            "before": n1, "after": n2,
            "second_exit": second["exit"], "second_error": is_error(second),
            "procs": {"first_resume": first, "second_resume": second}}


def s6(s1r: dict):
    n = s1r["counts"]
    return {"id": "S6", "kind": "관찰", "thread_id": s1r["thread_id"], "pass": None, "counts": n,
            "procs": {}, "note": "S1 스레드의 pre_interrupt 기록 횟수 (예상 2)"}


def s7():
    t = tid()
    run("start", t)
    p1 = spawn("resume", t, "--approve", "--execute-seconds", "2")
    p2 = spawn("resume", t, "--approve", "--execute-seconds", "2")
    o1, _ = p1.communicate()
    o2, _ = p2.communicate()
    st = run("status", t)
    return {"id": "S7", "kind": "관찰", "thread_id": t, "pass": None, "counts": counts(t),
            "effects": effect_rows(t), "resume_pids": [p1.pid, p2.pid],
            "procs": {"resume1": {"exit": p1.returncode, "result": parse(o1)},
                      "resume2": {"exit": p2.returncode, "result": parse(o2)},
                      "status": st}}


def brief(r: dict) -> str:
    res = r["result"]
    if "error" in res:
        return f"exit={r['exit']} error={res['error'][:80]}"
    v = res.get("values", {})
    return f"exit={r['exit']} result={v.get('result')} next={res.get('next')} pending={'Y' if res.get('pending') else 'N'}"


def verdict_of(r: dict) -> str:
    if r["id"] == "S4" and not r["valid"]:
        return "INVALID"
    return {True: "PASS", False: "FAIL", None: "-"}[r["pass"]]


def print_round(results: list[dict]) -> None:
    print("\nID  구분  판정  외부쓰기  pre_interrupt  프로세스 관측")
    for r in results:
        print(f"{r['id']}  {r['kind']}  {verdict_of(r):7}  {r['counts']['external_write']:^8}  {r['counts']['pre_interrupt']:^13}")
        for name, p in r["procs"].items():
            if "marker_seen" in p:
                print(f"      {name}: exit={p['exit']} marker_seen={p['marker_seen']}")
            else:
                print(f"      {name}: {brief(p)}")
        if r["id"] == "S4" and not r["valid"]:
            print("      S4 무효(INVALID): 전제 불충족, 관측값을 해석 대상에서 제외한다")
        if r["id"] == "S5":
            b, a = r["before"], r["after"]
            print(f"      second_resume: exit={r['second_exit']} error={r['second_error']}")
            print(f"      재개 전 external_write={b['external_write']} pre_interrupt={b['pre_interrupt']}"
                  f" / 재개 후 external_write={a['external_write']} pre_interrupt={a['pre_interrupt']}")
            print(f"      관측: {r['outcome']}")
        if r["id"] == "S7":
            print(f"      resume pid: {r['resume_pids']}")
            print("      kind            pid      at")
            for e in r["effects"]:
                print(f"      {e['kind']:<15} {e['pid']:<8} {e['at']}")
            print(f"      겹침 근거(pre_interrupt 수): {r['counts']['pre_interrupt']}"
                  " (3이면 두 프로세스 모두 interrupt 체크포인트에서 approval을 다시 실행)")
        if r.get("note"):
            print(f"      note: {r['note']}")


def summarize(rounds: list[list[dict]]) -> None:
    n = len(rounds)
    print(f"\n=== 분포 요약 ({n}회) ===")
    ids = [r["id"] for r in rounds[0]]
    for sid in ids:
        rs = [next(r for r in rd if r["id"] == sid) for rd in rounds]
        line = f"{sid}"
        if rs[0]["pass"] is not None:
            line += f" PASS {sum(1 for r in rs if r['pass'])}/{n}"
        if sid == "S4":
            line += f" 유효 {sum(1 for r in rs if r['valid'])}/{n}"
        ew = dict(Counter(r["counts"]["external_write"] for r in rs))
        pre = dict(Counter(r["counts"]["pre_interrupt"] for r in rs))
        line += f" | external_write {fmt(ew)} | pre_interrupt {fmt(pre)}"
        if sid == "S5":
            line += f" | 관측 {fmt(dict(Counter(r['outcome'] for r in rs)))}"
        if sid == "S4":
            valid_ew = dict(Counter(r["counts"]["external_write"] for r in rs if r["valid"]))
            line += f" | 유효 회차 external_write {fmt(valid_ew)}"
        print(line)


def fmt(d: dict) -> str:
    return "{" + ", ".join(f"{k}: {v}회" for k, v in sorted(d.items(), key=lambda kv: (-kv[1], str(kv[0])))) + "}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    ensure_effects_table()
    rounds = []
    for i in range(args.repeat):
        print(f"\n##### 회차 {i + 1}/{args.repeat} #####")
        r1 = s1()
        results = [r1, s2(), s3(), s4(), s5(), s6(r1), s7()]
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
