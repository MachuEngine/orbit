"""SPIKE-005: Gmail 초안 발송(drafts.send)으로 발송 결과 대조 가능한지 확인.

스파이크 전용 코드이며 제품 코드로 쓰지 않는다. 설계: docs/spikes/SPIKE-005-gmail-draft-send-reconcile.md

주의:
- 실제 메일이 인증된 계정 본인에게 발송된다. 수신자 옵션은 의도적으로 없다.
- 한 번 실행에서 실제로 발송되는 메일은 `--count` + 1통(D6 포함)이다. 최대 6통.
- --dry-run 은 네트워크와 인증 파일 없이 메시지와 실행 계획만 출력한다.
- 토큰, 클라이언트 비밀, 전체 메일 주소는 출력·저장하지 않는다.
- 이 스파이크가 만든 초안만 삭제한다.
"""

from __future__ import annotations

import argparse
import base64
import json
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
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_CLIENT_FILE = REPO_ROOT / "secrets" / "gmail-oauth-client.json"
DEFAULT_TOKEN_FILE = REPO_ROOT / "secrets" / "gmail-token-compose.json"  # 004 토큰과 분리
SUBJECT_PREFIX = "[orbit-spike-005]"
EXEC_HEADER = "X-Orbit-Execution-Id"
METADATA_HEADERS = [EXEC_HEADER, "Subject"]
COUNT_MIN = 1
COUNT_MAX = 5  # D6 1통을 더해 최대 6통
D2_GET_REPEATS = 3
D2_GET_INTERVAL_SEC = 1.0
D1_POLL_INTERVAL_SEC = 0.5
D1_POLL_MAX_SEC = 30.0
SENT_SEARCH_INTERVAL_SEC = 1.0
SENT_SEARCH_MAX_SEC = 30.0
D3_CONFIRM_RECHECK_DELAY_SEC = 5.0
RECONCILE_INTERVAL_SEC = 0.5
RECONCILE_MAX_SEC = 10.0
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
    """추가 발송을 막기 위해 실행 전체를 중단해야 한다."""


def new_tag() -> str:
    return secrets.token_hex(4)


def subject_for_round(tag: str, round_no: int) -> str:
    return f"{SUBJECT_PREFIX} {tag} round {round_no}"


def subject_query(tag: str, round_no: int) -> str:
    return f'subject:"{subject_for_round(tag, round_no)}"'


def error_summary(exc: BaseException) -> str:
    """'<클래스>: <메시지 첫 줄>'. 토큰류로 보이는 문자열은 가리고 길이를 제한한다."""
    lines = str(exc).splitlines()
    first = lines[0] if lines else ""
    first = re.sub(r"(?i)(bearer\s+|access_token=|refresh_token=|client_secret=)\S+",
                   r"\1***", first)
    return f"{type(exc).__name__}: {first[:300]}"


def http_status(exc: BaseException) -> int | None:
    """googleapiclient.errors.HttpError 의 상태 코드. HttpError 가 아니면 None."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def is_definitive_client_error(exc: BaseException) -> bool:
    """서버가 요청을 처리하지 않았다고 볼 수 있는 4xx. 408, 429 와 5xx, 상태 불명은 제외한다."""
    status = http_status(exc)
    return status is not None and 400 <= status < 500 and status not in (408, 429)


def is_not_found(exc: BaseException) -> bool:
    return http_status(exc) == 404


def build_message(recipient: str, tag: str, round_no: int, execution_id: str) -> EmailMessage:
    """RFC 822 메시지. 헤더 값은 코드가 만든 값만 쓴다. Message-ID 는 지정하지 않는다."""
    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject_for_round(tag, round_no)
    msg[EXEC_HEADER] = execution_id
    msg.set_content("Orbit SPIKE-005 draft send test. Safe to delete.\n")
    return msg


def encode_raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def masked_headers(msg: EmailMessage) -> list[tuple[str, str]]:
    return [(k, mask_email(str(v)) if k.lower() == "to" else str(v)) for k, v in msg.items()]


def append_result(path: Path, record: dict[str, Any]) -> None:
    """JSONL 한 줄을 추가한다. 호출 즉시 닫으므로 이후 예외가 나도 남는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


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


def create_draft(service: Any, recipient: str, tag: str, round_no: int,
                 execution_id: str) -> dict[str, Any]:
    msg = build_message(recipient, tag, round_no, execution_id)
    return service.users().drafts().create(
        userId="me", body={"message": {"raw": encode_raw(msg)}}).execute()


def get_draft(service: Any, draft_id: str) -> dict[str, Any]:
    return service.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()


def delete_draft(service: Any, draft_id: str) -> None:
    service.users().drafts().delete(userId="me", id=draft_id).execute()


def send_draft(service: Any, draft_id: str) -> dict[str, Any]:
    return service.users().drafts().send(userId="me", body={"id": draft_id}).execute()


def draft_exists(service: Any, draft_id: str) -> bool:
    """drafts.get 가 성공하면 True, 404 면 False. 그 밖의 오류는 그대로 올린다."""
    try:
        get_draft(service, draft_id)
        return True
    except Exception as exc:
        if is_not_found(exc):
            return False
        raise


def get_message_meta(service: Any, gmail_id: str) -> dict[str, Any]:
    """messages.get(metadata). {'id','thread_id','label_ids','headers'(소문자 키)} 를 돌려준다."""
    resp = service.users().messages().get(
        userId="me", id=gmail_id, format="metadata", metadataHeaders=METADATA_HEADERS
    ).execute()
    return _meta(resp)


def _meta(resp: dict[str, Any]) -> dict[str, Any]:
    headers = resp.get("payload", {}).get("headers", [])
    return {
        "id": resp.get("id"),
        "thread_id": resp.get("threadId"),
        "label_ids": resp.get("labelIds") or [],
        "headers": {h["name"].lower(): h["value"] for h in headers},
    }


def search_sent(service: Any, query: str) -> list[str]:
    resp = service.users().messages().list(userId="me", q=query, labelIds=["SENT"]).execute()
    return [m["id"] for m in resp.get("messages", [])]


def find_sent_by_execution_id(service: Any, tag: str, round_no: int, execution_id: str,
                              *, max_sec: float = SENT_SEARCH_MAX_SEC,
                              interval: float = SENT_SEARCH_INTERVAL_SEC) -> dict[str, Any]:
    """제목 검색으로 보낸편지함 후보를 찾고 X-Orbit-Execution-Id 가 같은 메일 id 를 센다.

    검색 결과가 0건이면 max_sec 까지 폴링한다.
    """
    t0 = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        ids = search_sent(service, subject_query(tag, round_no))
        if ids or time.monotonic() - t0 >= max_sec:
            break
        time.sleep(interval)
    matching = []
    for mid in ids:
        meta = get_message_meta(service, mid)
        if meta["headers"].get(EXEC_HEADER.lower()) == execution_id:
            matching.append(mid)
    return {"candidates": len(ids), "matching_ids": matching,
            "matching_count": len(matching), "exactly_one": len(matching) == 1,
            "elapsed_sec": round(time.monotonic() - t0, 2), "attempts": attempts}


# ── D6: 대조 ─────────────────────────────────────────────


def reconcile(service: Any, draft_id: str, draft_message_id: str | None,
              draft_thread_id: str | None, execution_id: str, *,
              max_wait_sec: float = RECONCILE_MAX_SEC,
              interval: float = RECONCILE_INTERVAL_SEC,
              sent_at: float | None = None) -> dict[str, Any]:
    """발송 응답 없이 초안 정보만으로 발송 여부와 보낸 메일을 찾는다.

    1) drafts.get 이 성공하면 아직 초안이 있다 -> 미발송으로 본다(재호출 금지, 여기서는 판단만).
    2) 404 면 발송됨으로 추정하고, 초안의 message.id 로 messages.get, 실패하면 threadId 로
       threads.get 을 시도해 X-Orbit-Execution-Id 가 같고 SENT 라벨이 붙은 메일을 찾는다.
    """
    out: dict[str, Any] = {"draft_id": draft_id}
    # drafts.send 호출이 반환된 시각(sent_at, time.monotonic 기준)부터 첫 drafts.get 직전까지
    out["since_send_to_first_get_sec"] = (
        round(time.monotonic() - sent_at, 2) if sent_at is not None else None)
    if draft_exists(service, draft_id):
        out["verdict"] = "draft_still_exists_not_sent"
        out["found_message_id"] = None
        return out
    out["verdict"] = "presumed_sent"
    t0 = time.monotonic()
    attempts: list[dict[str, Any]] = []
    out["attempts"] = attempts
    while True:
        found = _try_find(service, draft_message_id, draft_thread_id, execution_id, attempts)
        if found is not None:
            out.update(found_message_id=found[0], found_via=found[1])
            break
        if time.monotonic() - t0 >= max_wait_sec:
            out.update(found_message_id=None, found_via=None)
            break
        time.sleep(interval)
    out["elapsed_sec"] = round(time.monotonic() - t0, 2)
    return out


def _matches(meta: dict[str, Any], execution_id: str) -> bool:
    return (meta["headers"].get(EXEC_HEADER.lower()) == execution_id
            and "SENT" in meta["label_ids"])


def _try_find(service: Any, message_id: str | None, thread_id: str | None,
              execution_id: str, attempts: list[dict[str, Any]]) -> tuple[str, str] | None:
    if message_id:
        rec: dict[str, Any] = {"method": "messages.get", "id": message_id}
        try:
            meta = get_message_meta(service, message_id)
            rec["matched"] = _matches(meta, execution_id)
            attempts.append(rec)
            if rec["matched"]:
                return meta["id"] or message_id, "messages.get(draft_message_id)"
        except Exception as exc:
            if not is_not_found(exc):
                raise
            rec["status"] = 404
            attempts.append(rec)
    if thread_id:
        rec = {"method": "threads.get", "id": thread_id}
        try:
            resp = service.users().threads().get(
                userId="me", id=thread_id, format="metadata", metadataHeaders=METADATA_HEADERS
            ).execute()
            hits = [m for m in (_meta(x) for x in resp.get("messages", []))
                    if _matches(m, execution_id)]
            rec["matched"] = len(hits) == 1
            rec["hits"] = len(hits)
            attempts.append(rec)
            if len(hits) == 1:
                return hits[0]["id"], "threads.get(draft_thread_id)"
        except Exception as exc:
            if not is_not_found(exc):
                raise
            rec["status"] = 404
            attempts.append(rec)
    return None


# ── 회차 ─────────────────────────────────────────────────


def _poll_draft_gone(service: Any, draft_id: str, interval: float, max_sec: float) -> dict[str, Any]:
    t0 = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        if not draft_exists(service, draft_id):
            return {"gone": True, "first_404_elapsed_sec": round(time.monotonic() - t0, 2),
                    "attempts": attempts}
        if time.monotonic() - t0 >= max_sec:
            return {"gone": False, "first_404_elapsed_sec": None, "attempts": attempts}
        time.sleep(interval)


def _fail(results: Path, round_no: int, stage: str, exc: BaseException, **extra: Any) -> RoundAborted:
    append_result(results, {"event": "round_error", "round": round_no, "stage": stage,
                            "error": error_summary(exc), **extra})
    return RoundAborted(f"{stage}: {error_summary(exc)}")


def d2_unsent_draft(service: Any, addr: str, tag: str, round_no: int, results: Path) -> None:
    """D2: 발송하지 않은 초안이 계속 조회되는지 본 뒤, 이 스파이크가 만든 초안을 삭제한다."""
    draft_id = None
    try:
        created = create_draft(service, addr, tag, round_no, str(uuid.uuid4()))
        draft_id = created.get("id")
        append_result(results, {"event": "d2_draft_created", "round": round_no,
                                "draft_id": draft_id})
        gets = []
        for i in range(D2_GET_REPEATS):
            if i:
                time.sleep(D2_GET_INTERVAL_SEC)
            gets.append(draft_exists(service, draft_id))
        delete_draft(service, draft_id)
        append_result(results, {"event": "d2_draft_deleted", "round": round_no,
                                "draft_id": draft_id})
        gone_after_delete = not draft_exists(service, draft_id)
        draft_id = None  # 삭제 완료
        rec = {"event": "d2", "round": round_no, "get_exists_each": gets,
               "all_exist": all(gets), "get_404_after_delete": gone_after_delete}
        append_result(results, rec)
        print(json.dumps(rec, ensure_ascii=False))
    except Exception as exc:
        if draft_id:  # 이 스파이크가 만든 미발송 초안이 남았으면 지우려고 시도한다
            try:
                delete_draft(service, draft_id)
            except Exception:
                pass
        raise _fail(results, round_no, "D2", exc, draft_id=draft_id) from None


def d1_to_d5_round(service: Any, addr: str, tag: str, round_no: int, results: Path, *,
                   d1_interval: float = D1_POLL_INTERVAL_SEC,
                   d1_max_sec: float = D1_POLL_MAX_SEC,
                   d3_recheck_delay: float = D3_CONFIRM_RECHECK_DELAY_SEC) -> dict[str, Any]:
    execution_id = str(uuid.uuid4())
    stage = "D1_create"
    draft_id = None
    try:
        created = create_draft(service, addr, tag, round_no, execution_id)
        draft_id = created.get("id")
        dmsg = created.get("message") or {}
        draft_message_id, draft_thread_id = dmsg.get("id"), dmsg.get("threadId")
        append_result(results, {"event": "d1_draft_created", "round": round_no, "tag": tag,
                                "execution_id": execution_id, "draft_id": draft_id,
                                "draft_message_id": draft_message_id,
                                "draft_thread_id": draft_thread_id})
        stage = "D1_send"
        # 발송 호출 직전부터 결과가 불명확해질 수 있다. 예외가 나도 재호출하지 않고 중단한다.
        sent = send_draft(service, draft_id)
        t_sent = time.monotonic()
        append_result(results, {"event": "d1_sent", "round": round_no, "draft_id": draft_id,
                                "sent_id": sent.get("id"), "sent_thread_id": sent.get("threadId"),
                                "sent_label_ids": sent.get("labelIds")})
        stage = "D1_poll"
        d1 = _poll_draft_gone(service, draft_id, d1_interval, d1_max_sec)
        # 응답 직후 첫 조회 결과이므로 elapsed 는 응답 수신 직후부터 잰 값에 가깝다.
        d1["first_404_elapsed_sec"] = (round(time.monotonic() - t_sent, 2)
                                       if d1["gone"] else None)
        append_result(results, {"event": "d1", "round": round_no, **d1})

        stage = "D3_resend"
        d3 = _resend(service, draft_id, results, round_no)
        append_result(results, {"event": "d3_resend", "round": round_no, **d3})
        if d3["succeeded"]:
            append_result(results, {"event": "d3_second_send_succeeded_abort", "round": round_no,
                                    "draft_id": draft_id})
            raise RoundAborted("D3: 같은 draft_id 재발송이 성공했다. 추가 발송을 막기 위해 중단한다")

        stage = "D4"
        d4 = {
            "draft_message_id": draft_message_id, "sent_id": sent.get("id"),
            "message_id_equal": draft_message_id == sent.get("id"),
            "draft_thread_id": draft_thread_id, "sent_thread_id": sent.get("threadId"),
            "thread_id_equal": draft_thread_id == sent.get("threadId"),
        }
        append_result(results, {"event": "d4", "round": round_no, **d4})

        stage = "D5"
        meta = get_message_meta(service, sent["id"])
        d5 = {
            "execution_id_expected": execution_id,
            "execution_id_received": meta["headers"].get(EXEC_HEADER.lower()),
            "execution_id_preserved": meta["headers"].get(EXEC_HEADER.lower()) == execution_id,
            "subject_preserved": meta["headers"].get("subject") == subject_for_round(tag, round_no),
            "label_ids": meta["label_ids"], "has_sent_label": "SENT" in meta["label_ids"],
            "thread_id_from_get": meta["thread_id"],
            "thread_id_equal_draft_thread": meta["thread_id"] == draft_thread_id,
        }
        append_result(results, {"event": "d5", "round": round_no, **d5})

        stage = "D3_confirm"
        first = find_sent_by_execution_id(service, tag, round_no, execution_id)
        time.sleep(d3_recheck_delay)
        d3c = find_sent_by_execution_id(service, tag, round_no, execution_id)
        d3c["first_matching_count"] = first["matching_count"]
        d3c["second_matching_count"] = d3c["matching_count"]  # 판정은 두 번째 값
        d3c["contains_sent_id"] = sent["id"] in d3c["matching_ids"]
        append_result(results, {"event": "d3_confirm", "round": round_no, **d3c})
        if d3c["matching_count"] > 1:
            append_result(results, {"event": "d3_duplicate_send_abort", "round": round_no,
                                    "first_matching_count": first["matching_count"],
                                    "second_matching_count": d3c["matching_count"]})
            raise RoundAborted(
                f"D3: 같은 execution id 의 보낸 메일이 {d3c['matching_count']}통이다(중복 발송). "
                "추가 발송을 막기 위해 중단한다")
    except RoundAborted:
        raise
    except Exception as exc:
        raise _fail(results, round_no, stage, exc, draft_id=draft_id) from None
    return {"round": round_no, "execution_id": execution_id, "d1": d1, "d3": d3, "d4": d4,
            "d5": d5, "d3_confirm": d3c}


def _resend(service: Any, draft_id: str, results: Path, round_no: int) -> dict[str, Any]:
    """같은 draft_id 로 drafts.send 를 다시 호출한다.

    확정적 4xx(408, 429 제외)만 실패 결과로 돌려준다. 5xx, 408, 429, 상태 불명은 서버에서
    처리됐을 수 있으므로 이유를 기록하고 RoundAborted 로 중단한다.
    """
    try:
        resp = send_draft(service, draft_id)
    except Exception as exc:
        if not is_definitive_client_error(exc):
            raise _fail(results, round_no, "D3_resend", exc, draft_id=draft_id,
                        http_status=http_status(exc),
                        ambiguous="재발송 결과가 불명확해 서버에서 처리됐을 수 있다") from None
        return {"succeeded": False, "http_status": http_status(exc),
                "reason": error_summary(exc)}
    return {"succeeded": True, "second_sent_id": resp.get("id")}


def d6_round(service: Any, addr: str, tag: str, round_no: int, results: Path, *,
             reconcile_max_wait: float = RECONCILE_MAX_SEC,
             sent_search_max_sec: float = SENT_SEARCH_MAX_SEC) -> dict[str, Any]:
    """D6: drafts.send 의 응답을 버리고 초안 정보만으로 대조한다."""
    execution_id = str(uuid.uuid4())
    stage = "D6_create"
    draft_id = None
    try:
        created = create_draft(service, addr, tag, round_no, execution_id)
        draft_id = created.get("id")
        dmsg = created.get("message") or {}
        draft_message_id, draft_thread_id = dmsg.get("id"), dmsg.get("threadId")
        # 초안 ID 만 기록한다. 응답은 기록하지 않는다.
        append_result(results, {"event": "d6_draft_created", "round": round_no, "tag": tag,
                                "execution_id": execution_id, "draft_id": draft_id,
                                "draft_message_id": draft_message_id,
                                "draft_thread_id": draft_thread_id})
        stage = "D6_send"
        send_draft(service, draft_id)  # 응답을 변수에 저장하지 않고 버린다("응답 유실" 흉내)
        t_sent = time.monotonic()
        append_result(results, {"event": "d6_send_called", "round": round_no,
                                "draft_id": draft_id})
        stage = "D6_reconcile"
        rec = reconcile(service, draft_id, draft_message_id, draft_thread_id, execution_id,
                        max_wait_sec=reconcile_max_wait, sent_at=t_sent)
        append_result(results, {"event": "d6_reconcile", "round": round_no, **rec})
        # 대조가 끝난 뒤에야 보낸편지함 제목 검색으로 실제 메일을 확인한다.
        stage = "D6_verify"
        actual = find_sent_by_execution_id(service, tag, round_no, execution_id,
                                           max_sec=sent_search_max_sec)
        ver = {**actual, "reconcile_found_message_id": rec.get("found_message_id"),
               "reconcile_matches_sent_folder": (
                   rec.get("found_message_id") is not None
                   and actual["matching_ids"] == [rec["found_message_id"]])}
        append_result(results, {"event": "d6_verify", "round": round_no, **ver})
    except RoundAborted:
        raise
    except Exception as exc:
        raise _fail(results, round_no, stage, exc, draft_id=draft_id) from None
    return {"round": round_no, "execution_id": execution_id, "reconcile": rec, "verify": ver}


# ── 명령 ────────────────────────────────────────────────


def planned_sends(count: int) -> int:
    return count + 1


def cmd_dry_run(args: argparse.Namespace) -> int:
    tag = new_tag()
    total = planned_sends(args.count)
    print("[dry-run] 네트워크와 인증을 사용하지 않는다.")
    print(f"[dry-run] 태그: {tag}, 실제 발송 예정: 총 {total}통 "
          f"(D1 회차 {args.count}통 + D6 1통, 수신자는 본인 1명)")
    for i in range(1, args.count + 1):
        print(f"[dry-run] round {i}")
        print("  D2: 미발송 초안 생성 -> drafts.get x3(1초 간격) -> drafts.delete -> 삭제 후 get 404 기록")
        print("  D1: 초안 생성 -> drafts.send -> drafts.get 0.5초 간격 최대 30초(첫 404 시간)")
        print("  D3: 같은 draft_id 로 drafts.send 재호출(성공하거나 5xx·408·429·상태 불명이면 즉시 전체 중단)")
        print("  D4: 초안 message.id/threadId 와 발송 응답 id/threadId 비교")
        print("  D5: messages.get(metadata) 로 X-Orbit-Execution-Id, Subject, SENT 라벨 확인")
        print("  D3 확인: 제목 검색(SENT)으로 이번 execution id 메일 수를 세고, 5초 뒤 다시 세어 두 번째 값이 1통인지(2통 이상이면 중단)")
        _print_headers(tag, i)
    n = args.count + 1
    print(f"[dry-run] round {n} (D6 응답 유실 시뮬레이션)")
    print("  초안 생성 -> drafts.send(응답 버림) -> reconcile(drafts.get 404 -> messages.get/threads.get)")
    print("  대조 후 보낸편지함 제목 검색으로 대조 결과 확인")
    _print_headers(tag, n)
    print(f"[dry-run] 총 발송 예정 수: {total}")
    return 0


def _print_headers(tag: str, round_no: int) -> None:
    msg = build_message(DRY_RUN_RECIPIENT, tag, round_no, str(uuid.uuid4()))
    for k, v in masked_headers(msg):
        print(f"  {k}: {v}")
    print(f"  검색어: {subject_query(tag, round_no)}")


def cmd_run(args: argparse.Namespace, service: Any, results: Path) -> None:
    addr = get_own_address(service)
    tag = new_tag()
    append_result(results, {"command": "run", "start": {
        "started_at": datetime.now(UTC).isoformat(), "tag": tag,
        "recipient": mask_email(addr), "count": args.count,
        "planned_sends": planned_sends(args.count)}})
    summaries = []
    for i in range(1, args.count + 1):
        d2_unsent_draft(service, addr, tag, i, results)
        rec = d1_to_d5_round(service, addr, tag, i, results)
        summaries.append(rec)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    d6 = d6_round(service, addr, tag, args.count + 1, results)
    print(json.dumps(d6, ensure_ascii=False, indent=2))
    summary = {
        "d1_draft_gone": [r["d1"]["gone"] for r in summaries],
        "d1_first_404_elapsed_sec": [r["d1"]["first_404_elapsed_sec"] for r in summaries],
        "d3_resend_succeeded": [r["d3"]["succeeded"] for r in summaries],
        "d3_resend_http_status": [r["d3"].get("http_status") for r in summaries],
        "d3_exactly_one_sent": [r["d3_confirm"]["exactly_one"] for r in summaries],
        "d4_message_id_equal": [r["d4"]["message_id_equal"] for r in summaries],
        "d4_thread_id_equal": [r["d4"]["thread_id_equal"] for r in summaries],
        "d5_execution_id_preserved": [r["d5"]["execution_id_preserved"] for r in summaries],
        "d5_has_sent_label": [r["d5"]["has_sent_label"] for r in summaries],
        "d6_verdict": d6["reconcile"]["verdict"],
        "d6_since_send_to_first_get_sec": d6["reconcile"]["since_send_to_first_get_sec"],
        "d6_found_via": d6["reconcile"].get("found_via"),
        "d6_matches_sent_folder": d6["verify"]["reconcile_matches_sent_folder"],
    }
    append_result(results, {"command": "run", "summary": summary})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="네트워크·인증 없이 메시지 헤더와 실행 계획만 출력한다")
    p.add_argument("--client-file", type=Path, default=DEFAULT_CLIENT_FILE)
    p.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    p.add_argument("--results-file", type=Path, default=None,
                   help="기본: runs/results-YYYYmmddTHHMMSS.jsonl")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("run", help="D1~D5 를 count 회 수행한 뒤 D6 1회 수행")
    s.add_argument("--count", type=int, default=3,
                   help=f"D1~D5 회차 수({COUNT_MIN}~{COUNT_MAX}, 기본 3). "
                        f"D6 1통이 더해져 실제 발송은 count+1통(최대 {COUNT_MAX + 1}통)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not COUNT_MIN <= args.count <= COUNT_MAX:
        parser.error(f"--count 는 {COUNT_MIN}~{COUNT_MAX} 이어야 한다")
    if args.dry_run:
        return cmd_dry_run(args)
    results = args.results_file or default_results_file()
    service = get_service(args.client_file, args.token_file)
    try:
        cmd_run(args, service, results)
    except RoundAborted as exc:
        print(f"중단: 추가 발송을 막기 위해 실행을 멈췄다 ({exc}). 결과 파일을 확인하라: {results}",
              file=sys.stderr)
        return 1
    print(f"결과 저장: {results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
