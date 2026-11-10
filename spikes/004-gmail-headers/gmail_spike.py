"""SPIKE-004: Gmail API 발송 시 앱이 넣은 헤더 보존 확인.

스파이크 전용 코드이며 제품 코드로 쓰지 않는다. 설계: docs/spikes/SPIKE-004-gmail-header-preservation.md

주의:
- 실제 메일이 인증된 계정 본인에게 발송된다. 수신자 옵션은 의도적으로 없다.
- --dry-run 은 네트워크와 인증 파일 없이 메시지만 만든다.
- 토큰, 클라이언트 비밀, 전체 메일 주소는 출력·저장하지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import secrets
import sys
import time
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_CLIENT_FILE = REPO_ROOT / "secrets" / "gmail-oauth-client.json"
DEFAULT_TOKEN_FILE = REPO_ROOT / "secrets" / "gmail-token.json"
METADATA_HEADERS = ["Message-ID", "X-Orbit-Execution-Id"]
SUBJECT_PREFIX = "[orbit-spike]"
MSGID_DOMAIN = "orbit.invalid"
POLL_INTERVAL_SEC = 1.0
POLL_MAX_SEC = 120.0
G5_RECHECK_DELAY_SEC = 30.0
COUNT_MAX = 10
DRY_RUN_RECIPIENT = "dryrun@example.invalid"


def default_results_file() -> Path:
    # runs/ 는 .gitignore 의 `runs/` 규칙에 걸려 git 에서 제외된다.
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    return HERE / "runs" / f"results-{ts}.jsonl"


def mask_email(addr: str) -> str:
    """a***@gmail.com 형태로 마스킹한다."""
    local, _, domain = addr.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


class RoundAborted(Exception):
    """발송 후 단계에서 예외가 나서 실행을 중단해야 한다."""


def new_tag() -> str:
    """실행마다 새로 만드는 Subject 태그. uuid_a/uuid_b 와 무관한 값이다."""
    return secrets.token_hex(4)


def subject_for_round(tag: str, round_no: int) -> str:
    return f"{SUBJECT_PREFIX} {tag} round {round_no}"


def error_summary(exc: BaseException) -> str:
    """'<클래스>: <메시지 첫 줄>'. 토큰류로 보이는 문자열은 가리고 길이를 제한한다."""
    lines = str(exc).splitlines()
    first = lines[0] if lines else ""
    first = re.sub(r"(?i)(bearer\s+|access_token=|refresh_token=|client_secret=)\S+",
                   r"\1***", first)
    return f"{type(exc).__name__}: {first[:300]}"


def build_message(
    recipient: str,
    tag: str,
    round_no: int,
    uuid_a: str,
    uuid_b: str,
    *,
    with_message_id: bool,
) -> tuple[EmailMessage, str | None]:
    """RFC 822 메시지를 만든다. (메시지, 지정한 Message-ID 또는 None)를 돌려준다.

    uuid_a 는 Message-ID, uuid_b 는 X-Orbit-Execution-Id 에만 쓴다.
    Subject 에는 tag 만 넣고, Subject 와 본문에는 두 값을 넣지 않는다.
    """
    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject_for_round(tag, round_no)
    message_id = None
    if with_message_id:
        message_id = f"<{uuid_a}@{MSGID_DOMAIN}>"
        msg["Message-ID"] = message_id
    msg["X-Orbit-Execution-Id"] = uuid_b
    msg.set_content("Orbit SPIKE-004 header preservation test. Safe to delete.\n")
    return msg, message_id


def encode_raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def masked_headers(msg: EmailMessage) -> list[tuple[str, str]]:
    out = []
    for k, v in msg.items():
        out.append((k, mask_email(str(v)) if k.lower() == "to" else str(v)))
    return out


# ── 인증과 API ───────────────────────────────────────────


def write_token(token_file: Path, content: str) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    if token_file.exists():
        os.chmod(token_file, 0o600)
    tmp = token_file.with_name(token_file.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, token_file)
    os.chmod(token_file, 0o600)


def check_token_scopes(token_file: Path) -> None:
    """저장된 토큰의 스코프가 SCOPES 와 정확히 같지 않으면 종료한다."""
    try:
        data = json.loads(token_file.read_text())
    except (OSError, ValueError):
        sys.exit(f"토큰 파일을 읽을 수 없다. 파일을 삭제하고 다시 인증하라: {token_file}")
    saved = data.get("scopes") if isinstance(data, dict) else None
    if not isinstance(saved, list) or set(saved) != set(SCOPES):
        sys.exit(
            "기존 토큰의 스코프가 이 스파이크의 SCOPES 와 다르다(더 넓거나 좁음). "
            f"사용하지 않는다. 토큰 파일({token_file})을 삭제하고 다시 인증하라."
        )


def get_service(client_file: Path, token_file: Path) -> Any:
    # 네트워크가 필요한 import 는 dry-run 에서 쓰지 않도록 지연 import 한다.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_file.exists():
        check_token_scopes(token_file)
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_file.exists():
                sys.exit(f"OAuth 클라이언트 파일이 없다: {client_file} (README 참고)")
            flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
            creds = flow.run_local_server(port=0)
        write_token(token_file, creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_own_address(service: Any) -> str:
    return service.users().getProfile(userId="me").execute()["emailAddress"]


def send_message(
    service: Any, recipient: str, tag: str, round_no: int, *, with_message_id: bool,
    results: Path | None = None,
) -> dict[str, Any]:
    uuid_a = str(uuid.uuid4())  # Message-ID 용
    uuid_b = str(uuid.uuid4())  # X-Orbit-Execution-Id 용 (uuid_a 와 다른 값)
    msg, message_id = build_message(
        recipient, tag, round_no, uuid_a, uuid_b, with_message_id=with_message_id
    )
    resp = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": encode_raw(msg)})
        .execute()
    )
    t0 = time.monotonic()  # 발송 응답 수신 직후
    if results is not None:
        # 후속 단계가 실패해도 발송 사실이 남도록 즉시 기록한다.
        append_result(results, {"event": "sent", "round": round_no,
                                "gmail_id": resp.get("id"), "uuid_a": uuid_a,
                                "uuid_b": uuid_b, "tag": tag})
    return {
        "round": round_no,
        "tag": tag,
        "uuid_a": uuid_a,
        "sent_message_id": message_id,
        "execution_id": uuid_b,
        "gmail_id": resp.get("id"),
        "thread_id": resp.get("threadId"),
        "label_ids": resp.get("labelIds"),
        "sent_at_monotonic": t0,
    }


def inspect_message(service: Any, gmail_id: str) -> dict[str, str]:
    resp = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=gmail_id,
            format="metadata",
            metadataHeaders=METADATA_HEADERS,
        )
        .execute()
    )
    headers = resp.get("payload", {}).get("headers", [])
    return {h["name"].lower(): h["value"] for h in headers}


def search_sent(service: Any, query: str) -> list[str]:
    """SENT 라벨 안에서 검색해 메시지 id 목록을 돌려준다."""
    resp = (
        service.users()
        .messages()
        .list(userId="me", q=query, labelIds=["SENT"])
        .execute()
    )
    return [m["id"] for m in resp.get("messages", [])]


def msgid_query(message_id: str) -> str:
    return f"rfc822msgid:{message_id.strip('<>')}"


def subject_query(tag: str, round_no: int) -> str:
    return f'subject:"{subject_for_round(tag, round_no)}"'


def compare_headers(
    expected_message_id: str | None,
    expected_execution_id: str | None,
    got: dict[str, str],
) -> dict[str, Any]:
    """받은 헤더를 기대값과 비교한다. 기대값이 None 이면 해당 비교는 하지 않는다. Subject 는 다루지 않는다."""
    got_mid = got.get("message-id")
    got_xid = got.get("x-orbit-execution-id")
    return {
        "message_id_expected": expected_message_id,
        "message_id_received": got_mid,
        "message_id_preserved": (
            got_mid == expected_message_id if expected_message_id else None
        ),
        "x_orbit_execution_id_expected": expected_execution_id,
        "x_orbit_execution_id_received": got_xid,
        "x_orbit_execution_id_preserved": (
            got_xid == expected_execution_id if expected_execution_id else None
        ),
    }


def poll_until_found(
    service: Any, query: str, t0: float, interval: float, max_sec: float,
    sent_id: str | None,
) -> dict[str, Any]:
    """sent_id 가 결과에 들어 있을 때만 found. sent_id 가 None 이면 1건 이상이면 found."""
    attempts = 0
    while True:
        attempts += 1
        ids = search_sent(service, query)
        contains = (sent_id in ids) if sent_id else None
        hit = contains if sent_id else len(ids) >= 1
        elapsed = time.monotonic() - t0
        if hit:
            return {"found": True, "count": len(ids), "contains_sent_id": contains,
                    "count_is_1": len(ids) == 1,
                    "elapsed_sec": round(elapsed, 2), "attempts": attempts}
        if elapsed >= max_sec:
            return {"found": False, "count": len(ids), "contains_sent_id": contains,
                    "count_is_1": False,
                    "elapsed_sec": round(elapsed, 2), "attempts": attempts}
        time.sleep(interval)


def g5_queries(execution_id: str, uuid_a: str | None) -> dict[str, str]:
    q = {
        "raw_value": execution_id,
        "quoted_value": f'"{execution_id}"',
        "header_operator": f"X-Orbit-Execution-Id:{execution_id}",
        "header_operator_quoted": f'"X-Orbit-Execution-Id: {execution_id}"',
    }
    if uuid_a:
        # Message-ID 값이 rfc822msgid: 없이 일반 색인 대상인지 보는 추가 대조
        q["msgid_value_plain_term"] = uuid_a
    return q


def run_g5(
    service: Any,
    execution_id: str,
    uuid_a: str | None,
    control: tuple[str, int] | None,
    sent_id: str | None,
    *,
    control_wait_sec: float = POLL_MAX_SEC,
    recheck_delay_sec: float = G5_RECHECK_DELAY_SEC,
) -> dict[str, Any]:
    """uuid_b(execution_id) 로만 헤더 검색을 시험한다.

    control 은 (subject_tag, round_no). 대조군은 태그가 든 Subject 검색이고,
    sent_id 가 결과에 들어 있을 때까지 폴링한 뒤 질의한다. 방금 보낸 메일이
    결과에 없는 질의(0건 포함)는 recheck_delay_sec 뒤 한 번 더 확인한다.
    """
    out: dict[str, Any] = {}
    if control is not None:
        tag, round_no = control
        ctl = poll_until_found(
            service, subject_query(tag, round_no), time.monotonic(),
            POLL_INTERVAL_SEC, control_wait_sec, sent_id,
        )
        out["control_subject"] = ctl
        out["g5_valid"] = ctl["found"]
    else:
        out["g5_valid"] = None  # 대조군 없이 실행(단독 search)
    queries = g5_queries(execution_id, uuid_a)

    def probe(q: str) -> dict[str, Any]:
        ids = search_sent(service, q)
        return {"count": len(ids),
                "contains_sent_id": (sent_id in ids) if sent_id else None}

    first = {name: probe(q) for name, q in queries.items()}

    def missed(r: dict[str, Any]) -> bool:
        return (not r["contains_sent_id"]) if sent_id else r["count"] == 0

    redo = [n for n, r in first.items() if missed(r)]
    second: dict[str, dict[str, Any]] = {}
    if redo:
        time.sleep(recheck_delay_sec)
        second = {n: probe(queries[n]) for n in redo}
    out["queries"] = {
        n: {"first": first[n],
            "recheck_after_sec": recheck_delay_sec if n in second else None,
            "second": second.get(n)}
        for n in queries
    }
    return out


# ── 명령 ────────────────────────────────────────────────


def dry_run_one(tag: str, round_no: int, *, with_message_id: bool) -> None:
    uuid_a, uuid_b = str(uuid.uuid4()), str(uuid.uuid4())
    msg, message_id = build_message(
        DRY_RUN_RECIPIENT, tag, round_no, uuid_a, uuid_b, with_message_id=with_message_id
    )
    raw = encode_raw(msg)
    print(f"[dry-run] round {round_no}")
    for k, v in masked_headers(msg):
        print(f"  {k}: {v}")
    print(f"  (지정한 Message-ID: {message_id})")
    print(f"  raw base64url 길이: {len(raw)}")
    print(f"  rfc822msgid 검색어: {msgid_query(message_id) if message_id else '(해당 없음)'}")
    print(f"  태그: {tag}")
    print(f"  대조군 검색어: {subject_query(tag, round_no)}")


def cmd_dry_run(args: argparse.Namespace) -> int:
    if args.command not in ("send", "run"):
        print("dry-run은 send/run에서만 의미가 있다.", file=sys.stderr)
        return 2
    print("[dry-run] 네트워크와 인증을 사용하지 않는다.")
    tag = new_tag()
    if args.command == "send":
        dry_run_one(tag, 1, with_message_id=not args.no_message_id)
    else:
        for i in range(1, args.count + 1):
            dry_run_one(tag, i, with_message_id=True)
        dry_run_one(tag, args.count + 1, with_message_id=False)
    return 0


def cmd_send(args: argparse.Namespace, service: Any, results: Path) -> None:
    addr = get_own_address(service)
    sent = send_message(service, addr, new_tag(), 1,
                        with_message_id=not args.no_message_id, results=results)
    out = {k: v for k, v in sent.items() if k != "sent_at_monotonic"}
    out["recipient"] = mask_email(addr)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    append_result(results, {"command": "send", "result": out})


def cmd_inspect(args: argparse.Namespace, service: Any, results: Path) -> None:
    got = inspect_message(service, args.gmail_id)
    if args.message_id or args.execution_id:
        out = compare_headers(args.message_id, args.execution_id, got)
    else:
        # 기대값이 없으면 비교하지 않고 받은 헤더만 보여준다. Subject 는 다루지 않는다.
        out = {"message_id_received": got.get("message-id"),
               "x_orbit_execution_id_received": got.get("x-orbit-execution-id")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    append_result(results, {"command": "inspect", "result": out})


def cmd_search(args: argparse.Namespace, service: Any, results: Path) -> None:
    ids = search_sent(service, msgid_query(args.message_id))
    out: dict[str, Any] = {
        "rfc822msgid": {"count": len(ids),
                        "contains_sent_id": (args.gmail_id in ids) if args.gmail_id else None}}
    if args.execution_id:
        uuid_a = args.message_id.strip("<>").split("@")[0]
        control = (args.subject_tag, args.round) if args.subject_tag else None
        out["g5"] = run_g5(service, args.execution_id, uuid_a, control, args.gmail_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    append_result(results, {"command": "search", "result": out})


def one_round(service: Any, addr: str, tag: str, round_no: int, *, with_message_id: bool,
              interval: float, max_sec: float, results: Path) -> dict[str, Any]:
    sent = send_message(service, addr, tag, round_no, with_message_id=with_message_id,
                        results=results)  # 성공 즉시 sent 줄이 기록된다
    try:
        return _after_send(service, sent, tag, round_no, with_message_id=with_message_id,
                           interval=interval, max_sec=max_sec)
    except Exception as exc:
        append_result(results, {"event": "round_error", "round": round_no,
                                "gmail_id": sent["gmail_id"], "error": error_summary(exc)})
        raise RoundAborted(error_summary(exc)) from None


def _after_send(service: Any, sent: dict[str, Any], tag: str, round_no: int, *,
                with_message_id: bool, interval: float, max_sec: float) -> dict[str, Any]:
    t0 = sent["sent_at_monotonic"]
    gid = sent["gmail_id"]
    rec: dict[str, Any] = {
        "round": round_no,
        "tag": tag,
        "uuid_a": sent["uuid_a"],
        "execution_id": sent["execution_id"],
        "gmail_id": gid,
        "with_message_id": with_message_id,
    }
    # G2/G3: 검색과 검색되기까지 걸린 시간. t0 는 발송 응답 직후이므로 G1 조회보다 먼저 한다.
    if with_message_id:
        rec["g2_g3"] = poll_until_found(
            service, msgid_query(sent["sent_message_id"]), t0, interval, max_sec, gid
        )
    # G1: 헤더 조회 (메모리 값과 비교)
    got = inspect_message(service, gid)
    rec["g1"] = compare_headers(sent["sent_message_id"], sent["execution_id"], got)
    if not with_message_id:
        # G4: Gmail 이 만든 Message-ID 형식 기록
        rec["g4_generated_message_id"] = got.get("message-id")
        return rec
    # G5: uuid_b 로만 검색, 대조군은 태그가 든 Subject 검색
    rec["g5"] = run_g5(service, sent["execution_id"], sent["uuid_a"], (tag, round_no), gid)
    return rec


def cmd_run(args: argparse.Namespace, service: Any, results: Path) -> None:
    addr = get_own_address(service)
    tag = new_tag()
    started = {"started_at": datetime.now(UTC).isoformat(), "tag": tag,
               "recipient": mask_email(addr), "count": args.count, "gap": args.gap}
    append_result(results, {"command": "run", "start": started})
    rounds: list[dict[str, Any]] = []
    plan = [True] * args.count + [False]  # 마지막 1회는 Message-ID 미지정(G4)
    for i, with_mid in enumerate(plan):
        if i:
            time.sleep(args.gap)
        rec = one_round(service, addr, tag, i + 1, with_message_id=with_mid,
                        interval=POLL_INTERVAL_SEC, max_sec=POLL_MAX_SEC, results=results)
        append_result(results, {"command": "run", "round": rec})  # 라운드마다 즉시 저장
        rounds.append(rec)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    summary = {
        "g1_message_id_preserved": [r["g1"]["message_id_preserved"]
                                    for r in rounds if r["with_message_id"]],
        "g1_x_orbit_preserved": [r["g1"]["x_orbit_execution_id_preserved"] for r in rounds],
        "g2_found": [r["g2_g3"]["found"] for r in rounds if "g2_g3" in r],
        "g2_g3_count_is_1": [r["g2_g3"]["count_is_1"] for r in rounds if "g2_g3" in r],
        "g3_elapsed_sec": [r["g2_g3"]["elapsed_sec"] for r in rounds
                           if r.get("g2_g3", {}).get("found")],
        "g5_valid": [r["g5"]["g5_valid"] for r in rounds if "g5" in r],
    }
    append_result(results, {"command": "run", "summary": summary})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def append_result(path: Path, record: dict[str, Any]) -> None:
    """JSONL 한 줄을 추가한다. 호출 즉시 닫으므로 이후 예외가 나도 남는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="네트워크·인증 없이 메시지만 만들어 헤더를 출력한다(send/run 전용)")
    p.add_argument("--client-file", type=Path, default=DEFAULT_CLIENT_FILE)
    p.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    p.add_argument("--results-file", type=Path, default=None,
                   help="기본: runs/results-YYYYmmddTHHMMSS.jsonl (라운드마다 한 줄 추가)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("send", help="본인에게 테스트 메일 1통 발송")
    s.add_argument("--no-message-id", action="store_true", help="Message-ID 미지정 발송(G4)")
    s = sub.add_parser("inspect", help="Gmail id 로 헤더 조회(G1). 기대값을 주면 비교한다")
    s.add_argument("gmail_id")
    s.add_argument("--message-id", help="기대하는 Message-ID (예: <uuid@orbit.invalid>)")
    s.add_argument("--execution-id", help="기대하는 X-Orbit-Execution-Id")
    s = sub.add_parser("search", help="Message-ID 검색(G2)과 사용자 정의 헤더 검색(G5)")
    s.add_argument("message_id", help="예: <uuid@orbit.invalid>")
    s.add_argument("--execution-id", help="G5 에 쓸 X-Orbit-Execution-Id 값")
    s.add_argument("--subject-tag", help="대조군(Subject 검색)에 쓸 태그. --round 와 함께 준다")
    s.add_argument("--round", type=int, help="대조군(Subject 검색)에 쓸 회차 번호")
    s.add_argument("--gmail-id", help="방금 보낸 메일의 Gmail id. 있으면 contains_sent_id 를 기록한다")
    s = sub.add_parser("run", help="G1~G5 전체 수행")
    s.add_argument("--count", type=int, default=3,
                   help=f"Message-ID 지정 발송 횟수(1~{COUNT_MAX}, 기본 3)")
    s.add_argument("--gap", type=float, default=5.0, help="발송 사이 간격(초, 0 이상, 기본 5)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        if not 1 <= args.count <= COUNT_MAX:
            parser.error(f"--count 는 1~{COUNT_MAX} 이어야 한다")
        if not (math.isfinite(args.gap) and args.gap >= 0):
            parser.error("--gap 은 0 이상의 유한한 수여야 한다")
    if args.command == "search" and bool(args.subject_tag) != (args.round is not None):
        parser.error("--subject-tag 와 --round 는 함께 줘야 한다")
    if args.dry_run:
        return cmd_dry_run(args)
    results = args.results_file or default_results_file()
    service = get_service(args.client_file, args.token_file)
    handler = {"send": cmd_send, "inspect": cmd_inspect,
               "search": cmd_search, "run": cmd_run}[args.command]
    try:
        handler(args, service, results)
    except RoundAborted as exc:
        print(f"중단: 발송 후 단계 실패 ({exc}). 결과 파일의 round_error 를 확인하라: {results}",
              file=sys.stderr)
        return 1
    print(f"결과 저장: {results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
