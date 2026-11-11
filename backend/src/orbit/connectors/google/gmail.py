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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol

import httpx

from orbit.connectors.contracts import (
    ORBIT_EXECUTION_HEADER,
    Address,
    AttachmentMeta,
    ConnectorError,
    Draft,
    DraftSpec,
    InvalidQuery,
    MailMessage,
    MailThread,
    MessageSummary,
    NotFound,
    PermissionRevoked,
    RateLimited,
    ResultUnknown,
    SearchPage,
    TransientError,
)
from orbit.connectors.sim.mail_query import parse_query

DEFAULT_BASE_URL = "https://gmail.googleapis.com/gmail/v1"

_READ_ATTEMPTS = 3
_RETRY_DELAYS_S = (0.2, 0.5)
_MAX_RESULTS_CAP = 100
_RATE_LIMIT_REASONS = frozenset(
    {"ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded", "quotaexceeded"}
)
# 호출자에게 돌려주는 헤더 (대소문자 무시로 찾고 아래 표기로 돌려준다)
_KEPT_HEADERS = ("Message-ID", "In-Reply-To", "References", ORBIT_EXECUTION_HEADER)
_LABEL_OPERATORS = {"SENT": "sent", "INBOX": "inbox", "DRAFT": "drafts"}
_PLAIN_VALUE = re.compile(r"[\w.@]+")
_KEYWORDS = frozenset({"or", "and", "not", "around"})

SleepFn = Callable[[float], Awaitable[None]]


class AccessTokenProvider(Protocol):
    """액세스 토큰 공급자. 토큰 원문을 로그·예외에 남기지 않는다."""

    async def access_token(self) -> str: ...

    async def refresh(self) -> str:
        """토큰을 새로 받아 돌려준다. 실패(철회 포함)는 `PermissionRevoked`로 던진다."""
        ...


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

    def text(self) -> str:
        if self.plain:
            # Gmail은 줄바꿈을 CRLF로 저장하고 본문 끝에 줄바꿈을 붙인다. 초안 본문과 대조할 수
            # 있게 LF로 통일하고 끝의 줄바꿈은 뗀다(본문 끝의 줄바꿈 차이는 보존하지 않는다).
            text = "\n".join(self.plain).replace("\r\n", "\n").replace("\r", "\n")
            return text.rstrip("\n")
        if self.html:
            return html_to_text("\n".join(self.html))
        return ""


def _walk(part: Mapping[str, Any], acc: _Body) -> None:
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
            _walk(child, acc)
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


# ---------------------------------------------------------------- 오류 대응


def _error_info(response: httpx.Response) -> tuple[set[str], bool]:
    """(소문자 사유 코드 집합, invalid_grant 여부). 응답 본문 문장은 오류 메시지에 쓰지 않는다."""
    reasons: set[str] = set()
    revoked = False
    try:
        data = response.json()
    except ValueError:
        return reasons, revoked
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, str):
        revoked = error.lower() == "invalid_grant"
        reasons.add(error.lower())
    elif isinstance(error, dict):
        for item in error.get("errors") or []:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"].lower())
        status = error.get("status")
        if isinstance(status, str):
            reasons.add(status.lower())
        message = error.get("message")
        if isinstance(message, str) and "invalid_grant" in message.lower():
            revoked = True
    return reasons, revoked


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _map_error(response: httpx.Response, *, search: bool) -> ConnectorError:
    status = response.status_code
    reasons, revoked = _error_info(response)
    suffix = f" ({','.join(sorted(reasons))})" if reasons else ""
    if revoked:
        return PermissionRevoked(f"Gmail 인증 철회{suffix}")
    if status == 429 or reasons & _RATE_LIMIT_REASONS:
        return RateLimited(
            retry_after_s=_retry_after(response), message=f"Gmail 호출 한도 초과 HTTP {status}"
        )
    if status in (401, 403):
        return PermissionRevoked(f"Gmail 권한 없음 HTTP {status}{suffix}")
    if status == 404:
        return NotFound("Gmail에서 대상을 찾을 수 없다")
    if status == 400 and search:
        return InvalidQuery(f"Gmail이 검색어를 거절했다{suffix}")
    return ConnectorError(f"Gmail 요청 실패 HTTP {status}{suffix}")


# ---------------------------------------------------------------- 커넥터


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class GmailConnector:
    """`MailConnector` 계약의 Gmail 구현. 연결(계정) 하나에 묶인다."""

    def __init__(
        self,
        token_provider: AccessTokenProvider,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._tokens = token_provider
        self._base = base_url.rstrip("/")
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout or httpx.Timeout(15.0, connect=5.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GmailConnector:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- HTTP 공통 --------------------------------------------------------------

    async def _access_token(self) -> str:
        try:
            return await self._tokens.access_token()
        except ConnectorError:
            raise
        except Exception:
            raise TransientError("액세스 토큰을 가져오지 못했다") from None

    async def _refresh_token(self) -> str:
        try:
            return await self._tokens.refresh()
        except ConnectorError:
            raise
        except Exception:
            raise PermissionRevoked("액세스 토큰을 갱신하지 못했다") from None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Sequence[tuple[str, str]] | None = None,
        json: Mapping[str, Any] | None = None,
        write: bool = False,
        search: bool = False,
    ) -> Any:
        url = f"{self._base}/users/me/{path}"
        token = await self._access_token()
        refreshed = False
        failures = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=httpx.QueryParams(list(params)) if params else None,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TransportError as exc:
                before_send = isinstance(
                    exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout
                )
                if write and not before_send:
                    raise ResultUnknown("요청을 보낸 뒤 연결이 끊기거나 시간이 초과됐다") from None
                failures += 1
                if write or failures >= _READ_ATTEMPTS:
                    raise TransientError("Gmail에 연결하지 못했다") from None
                await self._sleep(_RETRY_DELAYS_S[min(failures, len(_RETRY_DELAYS_S)) - 1])
                continue
            status = response.status_code
            if status == 401:
                if refreshed:
                    raise PermissionRevoked("토큰을 갱신한 뒤에도 인증에 실패했다")
                token = await self._refresh_token()
                refreshed = True
                continue
            if status >= 500:
                if write:
                    raise ResultUnknown(f"쓰기 요청 뒤 서버 오류 HTTP {status}")
                failures += 1
                if failures >= _READ_ATTEMPTS:
                    raise TransientError(f"Gmail 서버 오류 HTTP {status}")
                await self._sleep(_RETRY_DELAYS_S[min(failures, len(_RETRY_DELAYS_S)) - 1])
                continue
            if 200 <= status < 300:
                if status == 204 or not response.content:
                    return None
                try:
                    return response.json()
                except ValueError:
                    if write:
                        raise ResultUnknown("쓰기 응답을 해석하지 못했다") from None
                    raise TransientError("Gmail 응답을 해석하지 못했다") from None
            raise _map_error(response, search=search)

    async def _get_resource(self, path: str, *, fmt: str = "full") -> Mapping[str, Any]:
        data = await self._request("GET", path, params=[("format", fmt)])
        if not isinstance(data, dict):
            raise TransientError("Gmail 응답 형식이 올바르지 않다")
        return data

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
        data = await self._request("GET", "messages", params=params, search=True)
        listing = data if isinstance(data, dict) else {}
        summaries: list[MessageSummary] = []
        for item in listing.get("messages") or []:
            try:
                resource = await self._get_resource(f"messages/{_quote(str(item['id']))}")
            except NotFound:
                continue  # 목록을 읽은 뒤 지워진 메시지
            summaries.append(parse_summary(resource))
        return SearchPage(
            messages=tuple(summaries), next_page_token=listing.get("nextPageToken") or None
        )

    async def get_message(self, message_id: str) -> MailMessage:
        return parse_message(await self._get_resource(f"messages/{_quote(message_id)}"))

    async def get_thread(self, thread_id: str) -> MailThread:
        resource = await self._get_resource(f"threads/{_quote(thread_id)}")
        messages = sorted(
            (parse_message(m) for m in resource.get("messages") or []), key=lambda m: m.sent_at
        )
        return MailThread(id=str(resource.get("id", thread_id)), messages=tuple(messages))

    # -- 초안·발송 --------------------------------------------------------------

    async def _reply_context(self, message_id: str) -> tuple[str, str | None, str | None]:
        """(스레드 ID, In-Reply-To로 쓸 Message-ID, References 값)."""
        data = await self._request(
            "GET",
            f"messages/{_quote(message_id)}",
            params=[
                ("format", "metadata"),
                ("metadataHeaders", "Message-ID"),
                ("metadataHeaders", "References"),
            ],
        )
        if not isinstance(data, dict) or "threadId" not in data:
            raise TransientError("Gmail 응답 형식이 올바르지 않다")
        headers = _headers_of(data.get("payload") or {})
        origin_id = headers.get("message-id")
        chain = " ".join(x for x in (headers.get("references"), origin_id) if x) or None
        return str(data["threadId"]), origin_id, chain

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
        data = await self._request("POST", "drafts", json={"message": message}, write=True)
        try:
            return Draft(
                draft_id=str(data["id"]),
                message_id=str(data["message"]["id"]),
                thread_id=str(data["message"]["threadId"]),
                spec=spec,
            )
        except (KeyError, TypeError):
            raise ResultUnknown("초안 생성 응답을 해석하지 못했다") from None

    async def get_draft(self, draft_id: str) -> Draft:
        data = await self._get_resource(f"drafts/{_quote(draft_id)}")
        message = data.get("message") or {}
        try:
            return Draft(
                draft_id=str(data["id"]),
                message_id=str(message["id"]),
                thread_id=str(message["threadId"]),
                spec=_spec_from_message(message),
            )
        except KeyError:
            raise TransientError("Gmail 응답 형식이 올바르지 않다") from None

    async def send_draft(self, draft_id: str) -> MailMessage:
        data = await self._request("POST", "drafts/send", json={"id": draft_id}, write=True)
        try:
            sent_id = str(data["id"])
        except (KeyError, TypeError):
            raise ResultUnknown("발송 응답을 해석하지 못했다") from None
        try:
            return await self.get_message(sent_id)
        except ConnectorError:
            # 발송은 끝났다. 재발송은 404이므로 호출자가 스레드 조회로 대조한다.
            raise ResultUnknown("발송은 요청됐지만 발송된 메일을 조회하지 못했다") from None

    async def delete_draft(self, draft_id: str) -> None:
        await self._request("DELETE", f"drafts/{_quote(draft_id)}", write=True)
