"""Gmail Adapter (ADR-0016 4절, ADR-0008 4절, SPIKE-004·005).

`MailConnector` 계약을 Gmail API v1 REST(httpx 비동기)로 구현한다. 인스턴스는 연결(계정) 하나에
묶이고, 액세스 토큰은 주입한 `AccessTokenProvider`에서 받는다.

Gmail 실제 동작에 맞춘 점 (SPIKE-004·005):
- 발송은 항상 초안을 거친다(`drafts.create` → `drafts.send`). 직접 발송·전달 메서드는 없다.
- Gmail은 `Message-ID`를 덮어쓰고 헤더 검색이 안 되므로 `X-Orbit-Execution-Id` 헤더를 초안에
  넣어 두고 스레드 조회로 대조한다. 보낸 메시지 ID는 초안 메시지 ID와 다르고 스레드 ID는 같다.
- 발송된 초안·삭제된 초안의 `drafts.get`·`drafts.send`는 404이고 `NotFound`로 옮긴다.
- `get_thread`는 Gmail이 돌려주는 대로 초안 메시지를 포함할 수 있다(`DRAFT` 라벨로 거른다).

오류 대응 (ADR-0016 4절):
- 429·`rateLimitExceeded` 계열 -> `RateLimited`, 401은 `refresh()` 후 한 번만 다시 시도하고 또
  401이면 `PermissionRevoked`, `invalid_grant`·권한 부족(403) -> `PermissionRevoked`, 404 ->
  `NotFound`, 검색의 400 -> `InvalidQuery`.
- 읽기의 5xx·전송 오류는 짧게 재시도한 뒤 `TransientError`.
- 쓰기(`create_draft`·`send_draft`·`delete_draft`)는 재시도하지 않는다. 요청을 보낸 뒤의
  타임아웃·연결 끊김·5xx는 `ResultUnknown`, 연결 전 실패는 `TransientError`다(ADR-0003).

오류 메시지에는 HTTP 상태와 Google의 오류 사유 코드만 넣는다. 메일 본문·주소·토큰·서버 응답
본문은 넣지 않는다.

색인용 읽기(ADR-0019 §2): `labels.list`, `messages.list`(`labelIds` + `q=after:<epoch초>`),
`users.getProfile`의 `historyId`, `history.list`. `history.list`의 404는 이력 만료라
`HistoryExpired`로 옮긴다(다른 읽기의 404는 `NotFound`다). 새 OAuth 권한은 필요 없다
(`gmail.readonly`).

본문은 `text/plain`을 우선하고 없을 때만 HTML을 텍스트로 바꾼다(스크립트·스타일 제거, 링크·이미지를
읽거나 가져오지 않는다). 첨부 내용은 내려받지 않는다. 검색 결과의 각 메시지는 `messages.get`을
`format=full`로 순차 조회한다(첨부 유무를 알기 위해서다).
"""

from __future__ import annotations

import asyncio
import base64
import html
import re
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

import httpx

from orbit.connectors.contracts import (
    ORBIT_EXECUTION_HEADER,
    Address,
    AttachmentMeta,
    Draft,
    DraftSpec,
    HistoryExpired,
    HistoryPage,
    MailLabel,
    MailMessage,
    MailThread,
    MessageIdPage,
    MessageSummary,
    NotFound,
    ResultUnknown,
    SearchPage,
    TransientError,
)
from orbit.connectors.google._http import GoogleHttp, Sleep
from orbit.connectors.google.tokens import AccessTokenProvider
from orbit.connectors.sim.mail_query import parse_query

DEFAULT_BASE_URL = "https://gmail.googleapis.com/gmail/v1"

_READ_ATTEMPTS = 3
_RETRY_DELAYS_S = (0.2, 0.5)
_MAX_RESULTS_CAP = 100
_LIST_PAGE_SIZE = 100
_HISTORY_PAGE_SIZE = 500
_HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")
_RATE_LIMIT_REASONS = frozenset(
    {"ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded", "quotaexceeded"}
)
# 호출자에게 돌려주는 헤더 (대소문자 무시로 찾고 아래 표기로 돌려준다)
_KEPT_HEADERS = ("Message-ID", "In-Reply-To", "References", ORBIT_EXECUTION_HEADER)
_LABEL_OPERATORS = {"SENT": "sent", "INBOX": "inbox", "DRAFT": "drafts"}
_PLAIN_VALUE = re.compile(r"[\w.@]+")
_KEYWORDS = frozenset({"or", "and", "not", "around"})

MAX_MIME_DEPTH = 20
MAX_BODY_CHARS = 200_000  # 본문 텍스트 글자 수 상한. 넘으면 잘라내고 표시를 붙인다
TRUNCATED_MARK = "\n[... 본문이 길어 일부를 잘랐다]"


# ---------------------------------------------------------------- 검색 질의 변환


def _render(value: str) -> str:
    if _PLAIN_VALUE.fullmatch(value) and value.lower() not in _KEYWORDS:
        return value
    return f'"{value}"'


def to_gmail_query(query: str) -> str:
    """계약의 검색 부분집합을 검증하고 Gmail `q` 문자열로 바꾼다.

    부분집합을 벗어나면 서버 호출 전에 `InvalidQuery`를 던진다. 초안은 `in:drafts`를 쓸 때만
    나오도록(Sim과 같게) `-in:drafts`를 붙인다. 입력 문자열을 그대로 넘기지 않고 파싱 결과로
    새로 만들므로 파서가 거른 연산자는 통과하지 못한다.
    """
    parsed = parse_query(query)
    parts: list[str] = []
    parts += [f"from:{_render(v)}" for v in parsed.froms]
    parts += [f"to:{_render(v)}" for v in parsed.tos]
    parts += [f"subject:{_render(v)}" for v in parsed.subjects]
    parts += [f"after:{d:%Y/%m/%d}" for d in parsed.after]
    parts += [f"before:{d:%Y/%m/%d}" for d in parsed.before]
    if parsed.has_attachment:
        parts.append("has:attachment")
    parts += [f"in:{_LABEL_OPERATORS[label]}" for label in parsed.labels]
    parts += [_render(v) for v in parsed.terms]
    if not parsed.wants_drafts:
        parts.append("-in:drafts")
    return " ".join(parts)


# ---------------------------------------------------------------- HTML -> 텍스트


class _TextExtractor(HTMLParser):
    """HTML에서 보이는 글자만 뽑는다. 스크립트·스타일 내용과 링크 주소·이미지는 버린다."""

    _SKIP = frozenset(
        {"script", "style", "head", "title", "noscript", "template", "iframe", "object", "svg"}
    )
    _BLOCK = frozenset(
        {"br", "p", "div", "li", "ul", "ol", "tr", "table", "blockquote", "hr"}
        | {"h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._out.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK and self._skip_depth == 0:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._out.append(data)

    def text(self) -> str:
        lines = [
            re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in "".join(self._out).split("\n")
        ]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(source: str) -> str:
    parser = _TextExtractor()
    parser.feed(source)
    parser.close()
    return parser.text()


# ---------------------------------------------------------------- 파싱


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_text(data: str, content_type: str) -> str:
    match = re.search(r"charset\s*=\s*\"?([\w.:-]+)", content_type, re.IGNORECASE)
    charset = match.group(1) if match else "utf-8"
    raw = _b64decode(data)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _headers_of(part: Mapping[str, Any]) -> dict[str, str]:
    """소문자 이름 -> 값 (같은 이름이 여럿이면 처음 것)."""
    out: dict[str, str] = {}
    for header in part.get("headers") or []:
        name = str(header.get("name", "")).lower()
        out.setdefault(name, str(header.get("value", "")))
    return out


def _decode_header_value(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 - 깨진 인코딩은 원문 그대로 쓴다
        return raw


def _addresses(raw: str | None) -> tuple[Address, ...]:
    if not raw:
        return ()
    result: list[Address] = []
    for name, email in getaddresses([raw]):
        if not email:
            continue
        display = _decode_header_value(name).strip() if name else ""
        result.append(Address(email=email, name=display or None))
    return tuple(result)


def _sent_at(resource: Mapping[str, Any], headers: Mapping[str, str]) -> datetime:
    internal = resource.get("internalDate")
    if internal is not None:
        try:
            return datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
        except (ValueError, OverflowError, OSError):
            pass
    date_header = headers.get("date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            pass
    return datetime.fromtimestamp(0, tz=UTC)


class _Body:
    def __init__(self) -> None:
        self.plain: list[str] = []
        self.html: list[str] = []
        self.attachments: list[AttachmentMeta] = []
        self.truncated = False

    def text(self) -> str:
        if self.plain:
            # Gmail은 줄바꿈을 CRLF로 저장하고 본문 끝에 줄바꿈을 붙인다. 초안 본문과 대조할 수
            # 있게 LF로 통일하고 끝의 줄바꿈은 뗀다(본문 끝의 줄바꿈 차이는 보존하지 않는다).
            text = "\n".join(self.plain).replace("\r\n", "\n").replace("\r", "\n")
            text = text.rstrip("\n")
        elif self.html:
            # 변환 전에 입력 크기도 제한한다(잘린 HTML 조각은 변환기가 알아서 닫는다).
            source = "\n".join(self.html)
            if len(source) > MAX_BODY_CHARS * 4:
                source = source[: MAX_BODY_CHARS * 4]
                self.truncated = True
            text = html_to_text(source)
        else:
            text = ""
        if len(text) > MAX_BODY_CHARS:
            text = text[:MAX_BODY_CHARS]
            self.truncated = True
        return text + TRUNCATED_MARK if self.truncated else text


def _walk(part: Mapping[str, Any], acc: _Body, depth: int = 0) -> None:
    if depth > MAX_MIME_DEPTH:
        acc.truncated = True
        return
    mime = str(part.get("mimeType", "")).lower()
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    children = part.get("parts") or []
    if filename:
        acc.attachments.append(
            AttachmentMeta(
                attachment_id=str(body.get("attachmentId") or part.get("partId") or ""),
                filename=str(filename),
                mime_type=mime or "application/octet-stream",
                size_bytes=int(body.get("size") or 0),
            )
        )
        return
    if children:
        for child in children:
            _walk(child, acc, depth + 1)
        return
    data = body.get("data")
    if not data:
        return
    content_type = _headers_of(part).get("content-type", mime)
    if mime == "text/plain":
        acc.plain.append(_decode_text(data, content_type))
    elif mime == "text/html":
        acc.html.append(_decode_text(data, content_type))


def parse_message(resource: Mapping[str, Any]) -> MailMessage:
    """`format=full` 메시지 리소스를 계약 모델로 바꾼다."""
    payload = resource.get("payload") or {}
    headers = _headers_of(payload)
    acc = _Body()
    _walk(payload, acc)
    kept = {name: headers[name.lower()] for name in _KEPT_HEADERS if name.lower() in headers}
    from_list = _addresses(headers.get("from"))
    return MailMessage(
        id=str(resource["id"]),
        thread_id=str(resource["threadId"]),
        from_=from_list[0] if from_list else Address(email=""),
        to=_addresses(headers.get("to")),
        cc=_addresses(headers.get("cc")),
        subject=_decode_header_value(headers.get("subject", "")),
        body_text=acc.text(),
        snippet=html.unescape(str(resource.get("snippet", ""))),
        sent_at=_sent_at(resource, headers),
        labels=tuple(str(x) for x in resource.get("labelIds") or ()),
        attachments=tuple(acc.attachments),
        headers=kept,
    )


def parse_summary(resource: Mapping[str, Any]) -> MessageSummary:
    msg = parse_message(resource)
    return MessageSummary(
        id=msg.id,
        thread_id=msg.thread_id,
        from_=msg.from_,
        subject=msg.subject,
        snippet=msg.snippet,
        sent_at=msg.sent_at,
        labels=msg.labels,
        has_attachment=msg.has_attachment,
    )


def _spec_from_message(resource: Mapping[str, Any]) -> DraftSpec:
    """초안 메시지에서 `DraftSpec`을 되살린다. 답장 대상 메시지 ID는 알 수 없어 `None`이다."""
    msg = parse_message(resource)
    return DraftSpec(
        to=msg.to,
        cc=msg.cc,
        subject=msg.subject,
        body_text=msg.body_text,
        reply_to_message_id=None,
        execution_id=msg.headers.get(ORBIT_EXECUTION_HEADER),
    )


# ---------------------------------------------------------------- MIME 생성


def _safe_header(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("헤더 값에 줄바꿈을 넣을 수 없다")
    return value


def _format_address(address: Address) -> str:
    email = _safe_header(address.email)
    if not email or any(c in email for c in " ,;<>\"'()"):
        raise ValueError("올바르지 않은 이메일 주소")
    if address.name:
        return formataddr((_safe_header(address.name), email), charset="utf-8")
    return email


def build_mime(
    spec: DraftSpec, *, in_reply_to: str | None = None, references: str | None = None
) -> bytes:
    """초안을 RFC 822 메시지로 만든다. `From`·`Message-ID`·`Date`는 Gmail이 채운다."""
    msg = EmailMessage(policy=policy.SMTP)
    if spec.to:
        msg["To"] = ", ".join(_format_address(a) for a in spec.to)
    if spec.cc:
        msg["Cc"] = ", ".join(_format_address(a) for a in spec.cc)
    msg["Subject"] = _safe_header(spec.subject)
    for name, value in spec.headers.items():
        msg[name] = _safe_header(value)
    if in_reply_to:
        msg["In-Reply-To"] = _safe_header(in_reply_to)
    if references:
        msg["References"] = _safe_header(references)
    msg.set_content(spec.body_text, charset="utf-8", cte="base64")
    return msg.as_bytes()


# ---------------------------------------------------------------- 커넥터


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _parsed[T](fn: Callable[[Mapping[str, Any]], T], resource: Mapping[str, Any]) -> T:
    """읽기 경로 파싱 오류는 응답 내용을 담지 않고 `TransientError`로 감싼다."""
    try:
        return fn(resource)
    except Exception:
        raise TransientError("Gmail 응답을 해석하지 못했다") from None


class GmailConnector:
    """`MailConnector` 계약의 Gmail 구현. 연결(계정) 하나에 묶인다.

    HTTP 규칙(토큰 갱신, 쓰기 결과 불명 판정, 오류 대응, `base_url` 검증)은 `_http.GoogleHttp`가
    Calendar 어댑터와 같이 쓰는 공통부다.
    """

    def __init__(
        self,
        tokens: AccessTokenProvider,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        allow_test_base_url: bool = False,
        timeout_s: float = 15.0,
        read_retries: int = 2,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = GoogleHttp(
            tokens,
            service="Gmail",
            base_url=base_url,
            client=client,
            allow_test_base_url=allow_test_base_url,
            timeout_s=timeout_s,
            read_retries=read_retries,
            sleep=sleep,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> GmailConnector:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(
        self,
        path: str,
        *,
        op: str,
        params: list[tuple[str, str]] | None = None,
        query: bool = False,
    ) -> dict[str, Any]:
        return await self._http.request(
            "GET", f"/users/me/{path}", op=op, write=False, params=params, query=query
        )

    async def _get_full(self, path: str, *, op: str) -> dict[str, Any]:
        return await self._get(path, op=op, params=[("format", "full")])

    # -- 검색·조회 --------------------------------------------------------------

    async def search_messages(
        self, query: str, *, max_results: int = 20, page_token: str | None = None
    ) -> SearchPage:
        if max_results < 1:
            raise ValueError("max_results는 1 이상이어야 한다")
        q = to_gmail_query(query)  # InvalidQuery는 여기서 서버 호출 전에 난다
        params = [("q", q), ("maxResults", str(min(max_results, _MAX_RESULTS_CAP)))]
        if page_token:
            params.append(("pageToken", page_token))
        listing = await self._get("messages", op="search_messages", params=params, query=True)
        summaries: list[MessageSummary] = []
        for item in listing.get("messages") or []:
            try:
                msg_id = str(item["id"])
            except (KeyError, TypeError):
                raise TransientError("Gmail 응답을 해석하지 못했다") from None
            try:
                resource = await self._get_full(f"messages/{_quote(msg_id)}", op="search_messages")
            except NotFound:
                continue  # 목록을 읽은 뒤 지워진 메시지
            summaries.append(_parsed(parse_summary, resource))
        token = listing.get("nextPageToken")
        return SearchPage(messages=tuple(summaries), next_page_token=str(token) if token else None)

    async def get_message(self, message_id: str) -> MailMessage:
        resource = await self._get_full(f"messages/{_quote(message_id)}", op="get_message")
        return _parsed(parse_message, resource)

    async def get_thread(self, thread_id: str) -> MailThread:
        resource = await self._get_full(f"threads/{_quote(thread_id)}", op="get_thread")

        def build(res: Mapping[str, Any]) -> MailThread:
            messages = sorted(
                (parse_message(m) for m in res.get("messages") or []), key=lambda m: m.sent_at
            )
            return MailThread(id=str(res.get("id", thread_id)), messages=tuple(messages))

        return _parsed(build, resource)

    # -- 초안·발송 --------------------------------------------------------------

    async def _reply_context(self, message_id: str) -> tuple[str, str | None, str | None]:
        """(스레드 ID, In-Reply-To로 쓸 Message-ID, References 값)."""
        data = await self._get(
            f"messages/{_quote(message_id)}",
            op="create_draft",
            params=[
                ("format", "metadata"),
                ("metadataHeaders", "Message-ID"),
                ("metadataHeaders", "References"),
            ],
        )

        def build(res: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
            headers = _headers_of(res.get("payload") or {})
            origin_id = headers.get("message-id")
            chain = " ".join(x for x in (headers.get("references"), origin_id) if x) or None
            return str(res["threadId"]), origin_id, chain

        return _parsed(build, data)

    async def create_draft(self, spec: DraftSpec) -> Draft:
        thread_id: str | None = None
        in_reply_to: str | None = None
        references: str | None = None
        if spec.reply_to_message_id is not None:
            thread_id, in_reply_to, references = await self._reply_context(spec.reply_to_message_id)
        mime = build_mime(spec, in_reply_to=in_reply_to, references=references)
        message: dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime).decode("ascii")}
        if thread_id is not None:
            message["threadId"] = thread_id
        data = await self._http.request(
            "POST", "/users/me/drafts", op="create_draft", write=True, body={"message": message}
        )
        try:
            return Draft(
                draft_id=str(data["id"]),
                message_id=str(data["message"]["id"]),
                thread_id=str(data["message"]["threadId"]),
                spec=spec,
            )
        except Exception:
            raise ResultUnknown("Gmail create_draft 응답을 해석하지 못했다") from None

    async def get_draft(self, draft_id: str) -> Draft:
        data = await self._get_full(f"drafts/{_quote(draft_id)}", op="get_draft")

        def build(res: Mapping[str, Any]) -> Draft:
            message = res["message"]
            return Draft(
                draft_id=str(res["id"]),
                message_id=str(message["id"]),
                thread_id=str(message["threadId"]),
                spec=_spec_from_message(message),
            )

        return _parsed(build, data)

    async def send_draft(self, draft_id: str) -> MailMessage:
        data = await self._http.request(
            "POST", "/users/me/drafts/send", op="send_draft", write=True, body={"id": draft_id}
        )
        try:
            return await self.get_message(str(data["id"]))
        except Exception:
            # 발송은 끝났다. 재발송은 404이므로 호출자가 스레드 조회로 대조한다.
            raise ResultUnknown("발송은 요청됐지만 발송된 메일을 확인하지 못했다") from None

    async def delete_draft(self, draft_id: str) -> None:
        await self._http.request(
            "DELETE",
            f"/users/me/drafts/{_quote(draft_id)}",
            op="delete_draft",
            write=True,
            allow_empty=True,
        )

    # -- 색인용 읽기 (ADR-0019 §2) -----------------------------------------------

    async def list_labels(self) -> list[MailLabel]:
        data = await self._get("labels", op="list_labels")

        def build(res: Mapping[str, Any]) -> list[MailLabel]:
            return [
                MailLabel(
                    id=str(item["id"]),
                    name=str(item.get("name", item["id"])),
                    kind="user" if item.get("type") == "user" else "system",
                )
                for item in res.get("labels") or []
            ]

        return _parsed(build, data)

    async def list_message_ids(
        self, label_id: str, *, after: datetime, page_token: str | None = None
    ) -> MessageIdPage:
        if after.tzinfo is None or after.utcoffset() is None:
            raise ValueError("after는 시간대가 있는 시각이어야 한다")
        params = [
            ("labelIds", label_id),
            ("q", f"after:{int(after.timestamp())}"),
            ("maxResults", str(_LIST_PAGE_SIZE)),
        ]
        if page_token:
            params.append(("pageToken", page_token))
        data = await self._get("messages", op="list_message_ids", params=params, query=True)

        def build(res: Mapping[str, Any]) -> MessageIdPage:
            token = res.get("nextPageToken")
            return MessageIdPage(
                message_ids=tuple(str(m["id"]) for m in res.get("messages") or []),
                next_page_token=str(token) if token else None,
            )

        return _parsed(build, data)

    async def current_history_id(self) -> str:
        data = await self._get("profile", op="current_history_id")

        def build(res: Mapping[str, Any]) -> str:
            return str(res["historyId"])

        return _parsed(build, data)

    async def list_history(
        self, start_history_id: str, *, page_token: str | None = None
    ) -> HistoryPage:
        params = [("startHistoryId", start_history_id), ("maxResults", str(_HISTORY_PAGE_SIZE))]
        params += [("historyTypes", t) for t in _HISTORY_TYPES]
        if page_token:
            params.append(("pageToken", page_token))
        try:
            data = await self._get("history", op="list_history", params=params)
        except NotFound:
            # Gmail은 보관 기간이 지난(또는 잘못된) 이력 위치에 404를 돌려준다.
            raise HistoryExpired("Gmail 변경 이력 위치가 만료됐다") from None

        def build(res: Mapping[str, Any]) -> HistoryPage:
            def ids(key: str) -> tuple[str, ...]:
                found: list[str] = []
                for record in res.get("history") or []:
                    for entry in record.get(key) or []:
                        found.append(str(entry["message"]["id"]))
                return tuple(dict.fromkeys(found))

            token = res.get("nextPageToken")
            return HistoryPage(
                history_id=str(res["historyId"]),
                added_ids=ids("messagesAdded"),
                deleted_ids=ids("messagesDeleted"),
                labels_added_ids=ids("labelsAdded"),
                labels_removed_ids=ids("labelsRemoved"),
                next_page_token=str(token) if token else None,
            )

        return _parsed(build, data)
