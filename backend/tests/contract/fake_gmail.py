"""상태가 있는 모의 Gmail API 서버 (`httpx.MockTransport` 핸들러).

계약 테스트가 실제 Google 없이 `GmailConnector`를 검증하려고 쓴다. 구현하는 범위는 아래뿐이고,
나머지는 404를 돌려준다. 어댑터 코드를 import하지 않고 Gmail 리소스 모양(JSON)만 따른다.

- `messages.list`(`q`, `maxResults`, `pageToken`), `messages.get`(`format=full|metadata`)
- `threads.get`, `drafts.create`·`get`·`send`·`delete`
- 검색 `q`: `from:`, `to:`(cc 포함), `subject:`, `after:`, `before:`, `has:attachment`,
  `in:sent|inbox|drafts`, `-in:drafts`, 키워드와 큰따옴표 구문. 모두 AND, 단어 단위 일치(Sim의 부분
  문자열 일치와 다르다). 색인 지연은 흉내 내지 않는다(생성 즉시 검색된다).
- Gmail 동작 재현(SPIKE-004·005): `Message-ID`를 서버가 새로 만든 값으로 덮어쓴다. 사용자 정의
  헤더는 보존한다. 초안은 `DRAFT` 라벨 메시지로 저장되고 스레드 조회에 포함된다. 발송하면 초안과
  초안 메시지가 사라지고 새 메시지 ID, 같은 스레드 ID로 `SENT` 메시지가 생긴다(본인에게 보내면
  `INBOX`·`UNREAD`도). 발송된 초안을 다시 발송하거나 조회하면 404다.
- 색인용 읽기(ADR-0019): `labels.list`, `messages.list`의 `labelIds`와 `q=after:<epoch초>`,
  `users.getProfile`(`historyId`), `history.list`(`startHistoryId`, `historyTypes`, `pageToken`,
  `maxResults`). 이력은 초안 생성·삭제와 발송에서 자동으로 남고, `add_message`·`remove_message`·
  `change_labels`로 직접 남길 수도 있다. `expire_history`로 만료시키면 그 이전 위치는 404다.
- 인증: `Authorization: Bearer <token>`이 `valid_token`과 다르면 401.
- 다른 계정으로의 배달은 흉내 내지 않는다.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses
from typing import Any, cast
from urllib.parse import unquote

import httpx

from orbit.connectors.sim.world import AccountState

_HISTORY_TYPE = {
    "messagesAdded": "messageAdded",
    "messagesDeleted": "messageDeleted",
    "labelsAdded": "labelAdded",
    "labelsRemoved": "labelRemoved",
}
_WORD = re.compile(r"\w+", re.UNICODE)
_CLAUSE = re.compile(r'(-?)(?:(\w+):)?("[^"]*"|\S+)')


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _error(status: int, reason: str, message: str = "fake") -> httpx.Response:
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}
    return httpx.Response(status, json=body)


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _contains_phrase(haystack: list[str], phrase: list[str]) -> bool:
    if not phrase:
        return True
    n = len(phrase)
    return any(haystack[i : i + n] == phrase for i in range(len(haystack) - n + 1))


class _Stored:
    """저장된 메시지 하나."""

    def __init__(
        self, msg_id: str, thread_id: str, labels: list[str], internal_ms: int, mime: Message
    ) -> None:
        self.id = msg_id
        self.thread_id = thread_id
        self.labels = labels
        self.internal_ms = internal_ms
        self.mime = mime

    def header(self, name: str) -> str:
        return str(self.mime.get(name, ""))

    def text(self) -> str:
        parts: list[str] = []
        for part in self.mime.walk():
            if part.get_content_maintype() == "text" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
        return "\n".join(parts)

    def has_attachment(self) -> bool:
        return any(part.get_filename() for part in self.mime.walk())


class FakeGmail:
    def __init__(
        self,
        account: AccountState,
        *,
        now: datetime,
        valid_token: str = "fake-token",  # noqa: S107
    ) -> None:
        self.email = account.email
        self.valid_token = valid_token
        self._now_ms = int(now.timestamp() * 1000)
        self._seq = 0
        self.messages: dict[str, _Stored] = {}
        self.drafts: dict[str, str] = {}  # draft_id -> message_id
        self.requests: list[tuple[str, str]] = []  # (method, path) 기록
        self.labels: list[dict[str, str]] = [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "SENT", "name": "SENT", "type": "system"},
            {"id": "DRAFT", "name": "DRAFT", "type": "system"},
            {"id": "Label_1", "name": "프로젝트", "type": "user"},
        ]
        self._history: list[dict[str, Any]] = []  # {"id": 번호, "type": ..., "message": ...}
        self.history_seq = 1000  # 시드 시점의 이력 위치
        self.history_floor = 0
        self.history_page_size: int | None = None  # 지정하면 maxResults 대신 이 크기로 나눈다
        self._seed(account)

    # -- 시드 -------------------------------------------------------------------

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:05d}"

    def _tick(self) -> int:
        self._now_ms += 1000
        return self._now_ms

    def _seed(self, account: AccountState) -> None:
        for m in account.messages.values():
            mime = EmailMessage()
            mime["From"] = f"{m.from_.name or ''} <{m.from_.email}>"
            if m.to:
                mime["To"] = ", ".join(f"{a.name or ''} <{a.email}>" for a in m.to)
            if m.cc:
                mime["Cc"] = ", ".join(f"{a.name or ''} <{a.email}>" for a in m.cc)
            mime["Subject"] = m.subject
            mime["Date"] = formatdate(m.sent_at.timestamp())
            mime["Message-ID"] = f"<{m.id}@fake.invalid>"
            for name, value in m.headers.items():
                mime[name] = value
            mime.set_content(m.body_text, charset="utf-8", cte="base64")
            for att in m.attachments:
                maintype, _, subtype = att.mime_type.partition("/")
                mime.add_attachment(
                    b"x" * att.size_bytes,
                    maintype=maintype,
                    subtype=subtype,
                    filename=att.filename,
                )
            self.messages[m.id] = _Stored(
                m.id, m.thread_id, list(m.labels), int(m.sent_at.timestamp() * 1000), mime
            )
        for d in account.drafts.values():
            assert d.spec is not None
            mime = EmailMessage()
            mime["From"] = account.email
            mime["To"] = ", ".join(a.email for a in d.spec.to)
            mime["Subject"] = d.spec.subject
            mime["Message-ID"] = f"<{d.message_id}@fake.invalid>"
            mime.set_content(d.spec.body_text, charset="utf-8", cte="base64")
            self.messages[d.message_id] = _Stored(
                d.message_id, d.thread_id, ["DRAFT"], self._tick(), mime
            )
            self.drafts[d.draft_id] = d.message_id

    # -- 변경 이력 --------------------------------------------------------------

    def _record(self, kind: str, msg: _Stored, labels: list[str] | None = None) -> None:
        self.history_seq += 1
        entry: dict[str, Any] = {
            "id": self.history_seq,
            "kind": kind,
            "message": {"id": msg.id, "threadId": msg.thread_id, "labelIds": list(msg.labels)},
        }
        if labels is not None:
            entry["labelIds"] = labels
        self._history.append(entry)

    def add_message(self, msg: _Stored) -> None:
        self.messages[msg.id] = msg
        self._record("messagesAdded", msg)

    def remove_message(self, message_id: str) -> None:
        self._record("messagesDeleted", self.messages.pop(message_id))

    def change_labels(self, message_id: str, *, add: list[str], remove: list[str]) -> None:
        msg = self.messages[message_id]
        added = [x for x in add if x not in msg.labels]
        removed = [x for x in remove if x in msg.labels]
        msg.labels = [x for x in msg.labels if x not in remove] + added
        if added:
            self._record("labelsAdded", msg, added)
        if removed:
            self._record("labelsRemoved", msg, removed)

    def expire_history(self) -> None:
        self.history_seq += 1
        self.history_floor = self.history_seq

    # -- 리소스 변환 ------------------------------------------------------------

    def _part(
        self, part: EmailMessage | Message, *, full: bool, wanted: set[str] | None
    ) -> dict[str, Any]:
        headers = [{"name": k, "value": str(v)} for k, v in part.items()]
        if wanted is not None:
            headers = [h for h in headers if h["name"].lower() in wanted]
        out: dict[str, Any] = {
            "mimeType": part.get_content_type(),
            "filename": part.get_filename() or "",
            "headers": headers,
        }
        if part.is_multipart():
            if full:
                out["parts"] = [
                    self._part(p, full=full, wanted=None)
                    for p in cast(list[Message], part.get_payload())
                ]
            return out
        payload = part.get_payload(decode=True)
        raw = payload if isinstance(payload, bytes) else b""
        if part.get_filename():
            out["body"] = {"attachmentId": f"att-{len(raw)}", "size": len(raw)}
        elif full:
            out["body"] = {"size": len(raw), "data": _b64(raw)}
        else:
            out["body"] = {"size": len(raw)}
        return out

    def _resource(self, msg: _Stored, fmt: str, wanted: set[str] | None = None) -> dict[str, Any]:
        full = fmt == "full"
        payload = self._part(msg.mime, full=full, wanted=None if full else wanted)
        return {
            "id": msg.id,
            "threadId": msg.thread_id,
            "labelIds": list(msg.labels),
            "internalDate": str(msg.internal_ms),
            "snippet": " ".join(msg.text().split())[:100],
            "payload": payload,
        }

    # -- 검색 -------------------------------------------------------------------

    def _matches(self, msg: _Stored, q: str) -> bool:
        wants_drafts = False
        for neg, op, raw in _CLAUSE.findall(q):
            value = raw.strip('"')
            if op == "in" and value == "drafts" and not neg:
                wants_drafts = True
        if "DRAFT" in msg.labels and not wants_drafts:
            return False
        for neg, op, raw in _CLAUSE.findall(q):
            value = raw.strip('"')
            phrase = _words(value)
            from_words = _words(msg.header("From"))
            recipient_words = _words(msg.header("To") + " " + msg.header("Cc"))
            if op == "in":
                if neg:
                    continue  # `-in:drafts`는 위에서 처리했다
                label = {"sent": "SENT", "inbox": "INBOX", "drafts": "DRAFT"}[value.lower()]
                ok = label in msg.labels
            elif op == "from":
                ok = _contains_phrase(from_words, phrase)
            elif op == "to":
                ok = _contains_phrase(recipient_words, phrase)
            elif op == "subject":
                ok = _contains_phrase(_words(msg.header("Subject")), phrase)
            elif op in ("after", "before"):
                if value.isdigit():  # 에포크 초
                    boundary = int(value) * 1000
                else:
                    y, mo, d = (int(x) for x in value.split("/"))
                    boundary = int(datetime(y, mo, d, tzinfo=UTC).timestamp() * 1000)
                ok = msg.internal_ms >= boundary if op == "after" else msg.internal_ms < boundary
            elif op == "has":
                ok = msg.has_attachment()
            elif op is None or op == "":
                hay = _words(msg.header("Subject")) + _words(msg.text()) + from_words
                ok = _contains_phrase(hay, phrase)
            else:
                raise ValueError(op)
            if ok == bool(neg):
                return False
        return True

    # -- 핸들러 -----------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.headers.get("authorization") != f"Bearer {self.valid_token}":
            return _error(401, "authError")
        marker = "/users/me/"
        path = unquote(request.url.path)
        rest = path[path.index(marker) + len(marker) :]
        params = request.url.params
        method = request.method
        try:
            if rest == "messages" and method == "GET":
                return self._list(params)
            if rest == "labels" and method == "GET":
                return httpx.Response(200, json={"labels": self.labels})
            if rest == "profile" and method == "GET":
                return httpx.Response(
                    200, json={"emailAddress": self.email, "historyId": str(self.history_seq)}
                )
            if rest == "history" and method == "GET":
                return self._history_list(params)
            if rest.startswith("messages/") and method == "GET":
                msg = self.messages.get(rest.split("/", 1)[1])
                if msg is None:
                    return _error(404, "notFound")
                wanted = {h.lower() for h in params.get_list("metadataHeaders")} or None
                return httpx.Response(
                    200, json=self._resource(msg, params.get("format", "full"), wanted)
                )
            if rest.startswith("threads/") and method == "GET":
                return self._get_thread(rest.split("/", 1)[1])
            if rest == "drafts" and method == "POST":
                return self._create_draft(json.loads(request.content))
            if rest == "drafts/send" and method == "POST":
                return self._send(json.loads(request.content)["id"])
            if rest.startswith("drafts/") and method == "GET":
                return self._get_draft(rest.split("/", 1)[1])
            if rest.startswith("drafts/") and method == "DELETE":
                return self._delete_draft(rest.split("/", 1)[1])
        except ValueError:
            return _error(400, "invalidArgument")
        return _error(404, "notFound")

    def _list(self, params: httpx.QueryParams) -> httpx.Response:
        q = params.get("q", "")
        wanted = params.get_list("labelIds")
        found = sorted(
            (
                m
                for m in self.messages.values()
                if self._matches(m, q) and all(x in m.labels for x in wanted)
            ),
            key=lambda m: (m.internal_ms, m.id),
            reverse=True,
        )
        offset = int(params["pageToken"].removeprefix("p:")) if "pageToken" in params else 0
        size = int(params.get("maxResults", "100"))
        page = found[offset : offset + size]
        body: dict[str, Any] = {"resultSizeEstimate": len(found)}
        if page:
            body["messages"] = [{"id": m.id, "threadId": m.thread_id} for m in page]
        if offset + size < len(found):
            body["nextPageToken"] = f"p:{offset + size}"
        return httpx.Response(200, json=body)

    def _history_list(self, params: httpx.QueryParams) -> httpx.Response:
        start = int(params["startHistoryId"])
        if start < self.history_floor or start > self.history_seq:
            return _error(404, "notFound")
        types = set(params.get_list("historyTypes")) or {
            "messageAdded",
            "messageDeleted",
            "labelAdded",
            "labelRemoved",
        }
        entries = [e for e in self._history if e["id"] > start]
        offset = int(params["pageToken"].removeprefix("h:")) if "pageToken" in params else 0
        size = self.history_page_size or int(params.get("maxResults", "100"))
        page = entries[offset : offset + size]
        records: list[dict[str, Any]] = []
        for e in page:
            if _HISTORY_TYPE[e["kind"]] not in types:
                continue
            item: dict[str, Any] = {"message": e["message"]}
            if "labelIds" in e:
                item["labelIds"] = e["labelIds"]
            records.append({"id": str(e["id"]), e["kind"]: [item]})
        body: dict[str, Any] = {"historyId": str(self.history_seq)}
        if records:
            body["history"] = records
        if offset + size < len(entries):
            body["nextPageToken"] = f"h:{offset + size}"
        return httpx.Response(200, json=body)

    def _get_thread(self, thread_id: str) -> httpx.Response:
        msgs = sorted(
            (m for m in self.messages.values() if m.thread_id == thread_id),
            key=lambda m: (m.internal_ms, m.id),
        )
        if not msgs:
            return _error(404, "notFound")
        body = {"id": thread_id, "messages": [self._resource(m, "full") for m in msgs]}
        return httpx.Response(200, json=body)

    def _create_draft(self, body: dict[str, Any]) -> httpx.Response:
        raw = body["message"]["raw"]
        mime = message_from_bytes(
            base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default
        )
        thread_id = body["message"].get("threadId")
        if thread_id is not None and not any(
            m.thread_id == thread_id for m in self.messages.values()
        ):
            return _error(404, "notFound")
        msg_id = self._next("m-new-")
        del mime["Message-ID"]  # 서버가 새로 만든 값으로 덮어쓴다 (SPIKE-004 G1)
        mime["Message-ID"] = f"<{msg_id}@mail.gmail.fake>"
        del mime["From"]
        mime["From"] = self.email
        stored = _Stored(msg_id, thread_id or self._next("t-new-"), ["DRAFT"], self._tick(), mime)
        self.add_message(stored)
        draft_id = self._next("r-new-")
        self.drafts[draft_id] = msg_id
        return httpx.Response(
            200,
            json={
                "id": draft_id,
                "message": {"id": msg_id, "threadId": stored.thread_id, "labelIds": ["DRAFT"]},
            },
        )

    def _get_draft(self, draft_id: str) -> httpx.Response:
        msg_id = self.drafts.get(draft_id)
        if msg_id is None:
            return _error(404, "notFound")
        return httpx.Response(
            200, json={"id": draft_id, "message": self._resource(self.messages[msg_id], "full")}
        )

    def _delete_draft(self, draft_id: str) -> httpx.Response:
        msg_id = self.drafts.pop(draft_id, None)
        if msg_id is None:
            return _error(404, "notFound")
        self.remove_message(msg_id)
        return httpx.Response(204)

    def _send(self, draft_id: str) -> httpx.Response:
        msg_id = self.drafts.pop(draft_id, None)
        if msg_id is None:
            return _error(404, "notFound")
        draft = self.messages[msg_id]
        self.remove_message(msg_id)
        new_id = self._next("m-sent-")
        mime = draft.mime
        del mime["Message-ID"]
        mime["Message-ID"] = f"<{new_id}@mail.gmail.fake>"
        del mime["Date"]
        mime["Date"] = formatdate(self._now_ms / 1000)
        labels = ["SENT"]
        addresses = [a for _, a in getaddresses([draft.header("To"), draft.header("Cc")])]
        if self.email.lower() in (a.lower() for a in addresses):
            labels += ["INBOX", "UNREAD"]
        sent = _Stored(new_id, draft.thread_id, labels, self._tick(), mime)
        self.add_message(sent)
        return httpx.Response(
            200, json={"id": new_id, "threadId": sent.thread_id, "labelIds": labels}
        )


def make_transport(fake: FakeGmail) -> httpx.MockTransport:
    handler: Callable[[httpx.Request], httpx.Response] = fake
    return httpx.MockTransport(handler)
