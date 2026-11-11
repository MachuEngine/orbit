"""GmailConnector 단위 테스트: MIME 생성·파싱, 검색 변환, 오류 매핑, 토큰 갱신, 재시도.

HTTP는 `httpx.MockTransport`로 응답을 지정한다(실제 Google 호출 없음). 계약 전체는
`tests/contract`의 `google-mock`이 검증한다. 데이터는 모두 합성이다.
"""

import base64
import json
from collections.abc import Callable
from email import message_from_bytes, policy
from email.header import decode_header, make_header

import httpx
import pytest

from orbit.connectors.contracts import (
    ORBIT_EXECUTION_HEADER,
    Address,
    ConnectorError,
    DraftSpec,
    InvalidQuery,
    NotFound,
    PermissionRevoked,
    RateLimited,
    ResultUnknown,
    TransientError,
)
from orbit.connectors.google.gmail import (
    MAX_BODY_CHARS,
    MAX_MIME_DEPTH,
    TRUNCATED_MARK,
    GmailConnector,
    build_mime,
    html_to_text,
    parse_message,
    to_gmail_query,
)

Handler = Callable[[httpx.Request], httpx.Response]


class Tokens:
    def __init__(self, *, refresh_fails: bool = False, refresh_error: Exception | None = None):
        self.current = "tok-1"
        self.refresh_calls = 0
        self._fails = refresh_fails
        self._error = refresh_error

    async def access_token(self) -> str:
        return self.current

    async def refresh(self) -> str:
        self.refresh_calls += 1
        if self._error is not None:
            raise self._error
        if self._fails:
            raise PermissionRevoked("revoked")
        self.current = f"tok-{self.refresh_calls + 1}"
        return self.current


class Harness:
    def __init__(self, handler: Handler, tokens: Tokens | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.tokens = tokens or Tokens()

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        client = httpx.AsyncClient(
            base_url="https://gmail.test/gmail/v1", transport=httpx.MockTransport(record)
        )
        self.gmail = GmailConnector(
            self.tokens, client=client, allow_test_base_url=True, sleep=sleep
        )


def err(status: int, reason: str = "backendError", message: str = "서버 메시지") -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}
    )


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def message_resource(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX"],
        "internalDate": "1762333200000",  # 2025-11-05T09:00:00Z
        "snippet": "합성 &amp; 요약",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "=?UTF-8?B?67Cl?= <bob@example.org>"},
                {"name": "To", "value": "alice@example.com"},
                {"name": "Subject", "value": "=?UTF-8?B?7ZWc6riAIOygnOuqqQ==?="},
                {"name": "X-Orbit-Execution-Id", "value": "exec-1"},
            ],
            "body": {"size": 5, "data": b64("합성 본문")},
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- MIME


def test_build_mime_encodes_korean_headers_and_keeps_orbit_header() -> None:
    spec = DraftSpec(
        to=(Address(email="bob@example.org", name="밥"),),
        cc=(Address(email="carol@example.org"),),
        subject="한글 제목 테스트",
        body_text="합성 본문입니다.\n둘째 줄",
        execution_id="exec-42",
    )
    raw = build_mime(spec)
    assert raw.isascii()  # 헤더는 RFC 2047, 본문은 base64
    parsed = message_from_bytes(raw, policy=policy.default)
    assert str(make_header(decode_header(parsed["Subject"]))) == "한글 제목 테스트"
    assert parsed["Cc"] == "carol@example.org"
    assert parsed[ORBIT_EXECUTION_HEADER] == "exec-42"
    assert parsed["From"] is None and parsed["Message-ID"] is None  # Gmail이 채운다
    assert parsed.get_content().replace("\r\n", "\n").strip() == "합성 본문입니다.\n둘째 줄"


def test_mime_round_trips_through_parse_message() -> None:
    spec = DraftSpec(
        to=(Address(email="bob@example.org", name="밥, 님"),),
        subject="왕복 확인",
        body_text="본문 한 줄",
        execution_id="exec-7",
    )
    raw = build_mime(spec)
    resource = {
        "id": "d1",
        "threadId": "t1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": k, "value": str(v)} for k, v in message_from_bytes(raw).items()],
            "body": {"data": base64.urlsafe_b64encode(_decoded_body(raw)).decode()},
        },
    }
    msg = parse_message(resource)
    assert msg.subject == "왕복 확인"
    assert msg.to == (Address(email="bob@example.org", name="밥, 님"),)
    assert msg.body_text == "본문 한 줄"  # 끝의 줄바꿈은 떼서 원문과 같다
    assert msg.headers[ORBIT_EXECUTION_HEADER] == "exec-7"


def _decoded_body(raw: bytes) -> bytes:
    payload = message_from_bytes(raw, policy=policy.default).get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload


def test_build_mime_reply_headers() -> None:
    raw = build_mime(
        DraftSpec(to=(Address(email="bob@example.org"),), subject="Re: x", body_text="b"),
        in_reply_to="<orig@mail.test>",
        references="<root@mail.test> <orig@mail.test>",
    )
    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["In-Reply-To"] == "<orig@mail.test>"
    assert parsed["References"] == "<root@mail.test> <orig@mail.test>"


@pytest.mark.parametrize(
    "spec",
    [
        DraftSpec(
            to=(Address(email="a@example.org"),), subject="x\r\nBcc: e@example.org", body_text="b"
        ),
        DraftSpec(
            to=(Address(email="a@example.org\nBcc: e@example.org"),), subject="x", body_text="b"
        ),
        DraftSpec(to=(Address(email="a b@example.org"),), subject="x", body_text="b"),
        DraftSpec(
            to=(Address(email="a@example.org", name="x\nBcc: e"),), subject="x", body_text="b"
        ),
    ],
)
def test_build_mime_rejects_header_injection(spec: DraftSpec) -> None:
    with pytest.raises(ValueError):
        build_mime(spec)


async def test_create_draft_reply_sets_thread_and_reply_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path.endswith("/users/me/messages/orig-1")
            assert request.url.params["format"] == "metadata"
            return httpx.Response(
                200,
                json={
                    "id": "orig-1",
                    "threadId": "thr-9",
                    "payload": {
                        "headers": [
                            {"name": "Message-Id", "value": "<orig@mail.test>"},
                            {"name": "References", "value": "<root@mail.test>"},
                        ]
                    },
                },
            )
        return httpx.Response(
            200, json={"id": "dr-1", "message": {"id": "m-1", "threadId": "thr-9"}}
        )

    h = Harness(handler)
    draft = await h.gmail.create_draft(
        DraftSpec(
            to=(Address(email="bob@example.org"),),
            subject="Re: 문의",
            body_text="답장",
            reply_to_message_id="orig-1",
            execution_id="exec-9",
        )
    )
    assert (draft.draft_id, draft.thread_id) == ("dr-1", "thr-9")
    posted = json.loads(h.requests[-1].content)["message"]
    assert posted["threadId"] == "thr-9"
    raw = base64.urlsafe_b64decode(posted["raw"])
    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["In-Reply-To"] == "<orig@mail.test>"
    assert parsed["References"] == "<root@mail.test> <orig@mail.test>"
    assert parsed[ORBIT_EXECUTION_HEADER] == "exec-9"


# ---------------------------------------------------------------- 파싱


def test_parse_message_decodes_headers_labels_and_snippet() -> None:
    msg = parse_message(message_resource())
    assert msg.subject == "한글 제목"
    assert msg.from_ == Address(email="bob@example.org", name="밥")
    assert msg.body_text == "합성 본문"
    assert msg.snippet == "합성 & 요약"
    assert msg.labels == ("INBOX",)
    assert msg.sent_at.isoformat() == "2025-11-05T09:00:00+00:00"
    assert msg.headers == {ORBIT_EXECUTION_HEADER: "exec-1"}


def test_parse_message_prefers_plain_and_lists_attachments() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "From", "value": "bob@example.org"}],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("평문 본문")}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>HTML 본문</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "report.pdf",
                "body": {"attachmentId": "att-1", "size": 123},
            },
        ],
    }
    msg = parse_message(message_resource(payload=payload))
    assert msg.body_text == "평문 본문"
    assert msg.has_attachment
    att = msg.attachments[0]
    assert (att.attachment_id, att.filename, att.mime_type, att.size_bytes) == (
        "att-1",
        "report.pdf",
        "application/pdf",
        123,
    )


def test_parse_message_html_only_strips_scripts_images_and_links() -> None:
    html_body = (
        "<html><head><title>제목</title><style>p{color:red}</style></head><body>"
        "<script>fetch('https://evil.invalid/x')</script>"
        "<p>안녕하세요 <a href='https://evil.invalid/track'>링크 글자</a></p>"
        "<img src='https://evil.invalid/pixel.png' alt='픽셀'>"
        "<div>둘째&nbsp;줄 &amp; 끝</div></body></html>"
    )
    payload = {"mimeType": "text/html", "body": {"data": b64(html_body)}, "headers": []}
    msg = parse_message(message_resource(payload=payload))
    assert "evil.invalid" not in msg.body_text
    assert "fetch" not in msg.body_text and "color" not in msg.body_text
    assert "픽셀" not in msg.body_text and "제목" not in msg.body_text
    assert msg.body_text == "안녕하세요 링크 글자\n\n둘째 줄 & 끝"


def test_html_to_text_ignores_unclosed_script() -> None:
    assert html_to_text("앞<script>alert(1)") == "앞"


def test_parse_message_uses_part_charset() -> None:
    data = base64.urlsafe_b64encode("한글".encode("euc-kr")).decode()
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="euc-kr"'}],
        "body": {"data": data},
    }
    assert parse_message(message_resource(payload=payload)).body_text == "한글"


# ---------------------------------------------------------------- 검색 변환


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("from:bob@example.org", "from:bob@example.org -in:drafts"),
        ("FROM:Bob@Example.org to:me", "from:bob@example.org to:me -in:drafts"),
        ('subject:"일정 문의"', 'subject:"일정 문의" -in:drafts'),
        ("after:2026/11/05 before:2026/11/06", "after:2026/11/05 before:2026/11/06 -in:drafts"),
        ("has:attachment in:inbox", "has:attachment in:inbox -in:drafts"),
        ("in:drafts zebra", "in:drafts zebra"),
        ('"alpha beta" gamma', '"alpha beta" gamma -in:drafts'),
        ("OR고양이", "or고양이 -in:drafts"),
        ('"or"', '"or" -in:drafts'),
    ],
)
def test_to_gmail_query(query: str, expected: str) -> None:
    assert to_gmail_query(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "a OR b",
        "-x",
        "label:inbox",
        "is:unread",
        "(a)",
        "rfc822msgid:x",
        "in:trash",
        "after:2026-1-1",
    ],
)
async def test_unsupported_query_raises_before_any_request(query: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("검증 전에 서버를 호출했다")

    h = Harness(handler)
    with pytest.raises(InvalidQuery):
        await h.gmail.search_messages(query)
    assert h.requests == []


async def test_gmail_400_on_search_is_invalid_query() -> None:
    h = Harness(lambda r: err(400, "invalidArgument"))
    with pytest.raises(InvalidQuery):
        await h.gmail.search_messages("hello")


# ---------------------------------------------------------------- 오류 매핑


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (err(429, "rateLimitExceeded"), RateLimited),
        (err(403, "userRateLimitExceeded"), RateLimited),
        (err(403, "dailyLimitExceeded"), ConnectorError),
        (err(403, "forbidden"), ConnectorError),
        (err(403, "authError"), PermissionRevoked),
        (err(403, "insufficientPermissions"), PermissionRevoked),
        (err(400, "failedPrecondition", "invalid_grant"), PermissionRevoked),
        (err(404, "notFound"), NotFound),
        (err(400, "invalidArgument"), ConnectorError),
    ],
)
async def test_status_mapping_for_reads(
    response: httpx.Response, expected: type[Exception]
) -> None:
    h = Harness(lambda r: response)
    with pytest.raises(expected) as info:
        await h.gmail.get_message("m1")
    assert type(info.value) is expected


async def test_rate_limited_carries_retry_after_without_retrying() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": 429}})

    h = Harness(handler)
    with pytest.raises(RateLimited) as info:
        await h.gmail.get_message("m1")
    assert info.value.retry_after_s == 7.0
    assert len(h.requests) == 1


@pytest.mark.parametrize("status", [400, 403, 404, 429])
async def test_error_messages_do_not_echo_server_text_or_tokens(status: int) -> None:
    secret = "비밀본문 bob@example.org tok-1"
    response = err(status, "someReason", secret)
    h = Harness(lambda r: response)
    with pytest.raises(ConnectorError) as info:
        await h.gmail.get_message("m1")
    assert secret not in str(info.value) and "tok-1" not in str(info.value)
    assert "bob@example.org" not in str(info.value)


async def test_read_retries_5xx_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return err(503) if calls < 3 else httpx.Response(200, json=message_resource())

    h = Harness(handler)
    assert (await h.gmail.get_message("m1")).id == "m1"
    assert calls == 3 and len(h.sleeps) == 2


async def test_read_5xx_exhausts_retries_as_transient() -> None:
    h = Harness(lambda r: err(500))
    with pytest.raises(TransientError):
        await h.gmail.get_message("m1")
    assert len(h.requests) == 3


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
async def test_read_transport_error_retries_then_transient(exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    h = Harness(handler)
    with pytest.raises(TransientError):
        await h.gmail.get_thread("t1")
    assert len(h.requests) == 3


# ---------------------------------------------------------------- 토큰 갱신


async def test_401_refreshes_once_and_retries_with_new_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer tok-1":
            return err(401, "authError")
        return httpx.Response(200, json=message_resource())

    h = Harness(handler)
    assert (await h.gmail.get_message("m1")).id == "m1"
    assert h.tokens.refresh_calls == 1
    assert [r.headers["authorization"] for r in h.requests] == ["Bearer tok-1", "Bearer tok-2"]


async def test_second_401_is_permission_revoked() -> None:
    h = Harness(lambda r: err(401, "authError"))
    with pytest.raises(PermissionRevoked):
        await h.gmail.get_message("m1")
    assert h.tokens.refresh_calls == 1 and len(h.requests) == 2


async def test_refresh_failure_is_permission_revoked() -> None:
    h = Harness(lambda r: err(401, "authError"), Tokens(refresh_fails=True))
    with pytest.raises(PermissionRevoked):
        await h.gmail.get_message("m1")
    h2 = Harness(lambda r: err(401, "authError"), Tokens(refresh_error=RuntimeError("tok-secret")))
    with pytest.raises(PermissionRevoked) as info:
        await h2.gmail.get_message("m1")
    assert "tok-secret" not in str(info.value)


async def test_write_401_refreshes_and_retries_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer tok-1":
            return err(401, "authError")
        return httpx.Response(204)

    h = Harness(handler)
    await h.gmail.delete_draft("dr-1")
    assert h.tokens.refresh_calls == 1


# ---------------------------------------------------------------- 쓰기 오류

SPEC = DraftSpec(to=(Address(email="bob@example.org"),), subject="s", body_text="b")


async def _write(h: Harness, op: str) -> None:
    if op == "create":
        await h.gmail.create_draft(SPEC)
    elif op == "send":
        await h.gmail.send_draft("dr-1")
    else:
        await h.gmail.delete_draft("dr-1")


@pytest.mark.parametrize("op", ["create", "send", "delete"])
@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x"), httpx.ReadError("x")]
)
async def test_write_failure_after_send_is_result_unknown_without_retry(
    op: str, exc: Exception
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    h = Harness(handler)
    with pytest.raises(ResultUnknown):
        await _write(h, op)
    assert len(h.requests) == 1 and h.sleeps == []


@pytest.mark.parametrize("op", ["create", "send", "delete"])
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_write_5xx_is_result_unknown_without_retry(op: str, status: int) -> None:
    h = Harness(lambda r: err(status))
    with pytest.raises(ResultUnknown):
        await _write(h, op)
    assert len(h.requests) == 1


@pytest.mark.parametrize("op", ["create", "send", "delete"])
@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ConnectTimeout("x")])
async def test_write_failure_before_send_is_transient(op: str, exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    h = Harness(handler)
    with pytest.raises(TransientError):
        await _write(h, op)
    assert len(h.requests) == 1


@pytest.mark.parametrize("op", ["send", "delete"])
async def test_write_404_is_not_found(op: str) -> None:
    h = Harness(lambda r: err(404, "notFound"))
    with pytest.raises(NotFound):
        await _write(h, op)


async def test_write_429_is_rate_limited_without_retry() -> None:
    h = Harness(lambda r: err(429, "rateLimitExceeded"))
    with pytest.raises(RateLimited):
        await h.gmail.send_draft("dr-1")
    assert len(h.requests) == 1


async def test_write_with_unparseable_success_body_is_result_unknown() -> None:
    h = Harness(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(ResultUnknown):
        await h.gmail.create_draft(SPEC)


async def test_send_succeeds_but_follow_up_read_fails_is_result_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "m-sent", "threadId": "t1"})
        return err(500)

    h = Harness(handler)
    with pytest.raises(ResultUnknown):
        await h.gmail.send_draft("dr-1")
    posts = [r for r in h.requests if r.method == "POST"]
    assert len(posts) == 1  # 재발송하지 않는다


async def test_send_returns_sent_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert json.loads(request.content) == {"id": "dr-1"}
            return httpx.Response(200, json={"id": "m1", "threadId": "t1"})
        return httpx.Response(200, json=message_resource(labelIds=["SENT"]))

    h = Harness(handler)
    sent = await h.gmail.send_draft("dr-1")
    assert "SENT" in sent.labels and sent.thread_id == "t1"


async def test_ids_are_percent_encoded_in_paths() -> None:
    h = Harness(lambda r: httpx.Response(200, json=message_resource()))
    await h.gmail.get_message("../drafts/x?y")
    assert "/users/me/messages/..%2Fdrafts%2Fx%3Fy" in str(h.requests[0].url)


async def test_search_requests_translated_query_and_skips_deleted_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/messages"):
            assert request.url.params["q"] == "from:bob@example.org -in:drafts"
            assert request.url.params["maxResults"] == "5"
            return httpx.Response(
                200,
                json={"messages": [{"id": "gone"}, {"id": "m1"}], "nextPageToken": "next-1"},
            )
        if path.endswith("/messages/gone"):
            return err(404, "notFound")
        return httpx.Response(200, json=message_resource())

    h = Harness(handler)
    page = await h.gmail.search_messages("from:bob@example.org", max_results=5)
    assert [m.id for m in page.messages] == ["m1"]
    assert page.next_page_token == "next-1"


async def test_get_draft_restores_spec() -> None:
    resource = message_resource(id="dm1", threadId="t5", labelIds=["DRAFT"])
    h = Harness(lambda r: httpx.Response(200, json={"id": "dr-5", "message": resource}))
    draft = await h.gmail.get_draft("dr-5")
    assert (draft.draft_id, draft.message_id, draft.thread_id) == ("dr-5", "dm1", "t5")
    assert draft.spec is not None
    assert draft.spec.subject == "한글 제목"
    assert draft.spec.execution_id == "exec-1"
    assert draft.spec.to == (Address(email="alice@example.com"),)


def test_draft_body_round_trips_exactly_with_crlf_normalization() -> None:
    body = "첫 줄\n\n셋째 줄"
    raw = build_mime(DraftSpec(to=(), subject="s", body_text=body))
    resource = {
        "id": "d",
        "threadId": "t",
        "payload": {
            "mimeType": "text/plain",
            "headers": [],
            "body": {"data": base64.urlsafe_b64encode(_decoded_body(raw)).decode()},
        },
    }
    assert parse_message(resource).body_text == body


# ---------------------------------------------------------------- 리뷰 1차 수정


async def test_other_403_is_plain_connector_error_with_reason_code() -> None:
    h = Harness(lambda r: err(403, "forbidden", "비밀 문장"))
    with pytest.raises(ConnectorError) as info:
        await h.gmail.get_message("m1")
    assert type(info.value) is ConnectorError
    assert "forbidden" in info.value.message and "비밀 문장" not in info.value.message


async def test_reason_code_is_length_limited() -> None:
    h = Harness(lambda r: err(403, "x" * 500))
    with pytest.raises(ConnectorError) as info:
        await h.gmail.get_message("m1")
    assert "x" * 50 not in info.value.message and len(info.value.message) < 100


@pytest.mark.parametrize("op", ["create", "send", "delete"])
async def test_write_408_is_result_unknown(op: str) -> None:
    h = Harness(lambda r: err(408, "requestTimeout"))
    with pytest.raises(ResultUnknown):
        await _write(h, op)
    assert len(h.requests) == 1


@pytest.mark.parametrize("op", ["create", "send", "delete"])
async def test_write_non_transport_request_error_is_result_unknown(op: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("x")

    h = Harness(handler)
    with pytest.raises(ResultUnknown):
        await _write(h, op)
    assert len(h.requests) == 1


async def test_read_non_transport_request_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("x")

    h = Harness(handler)
    with pytest.raises(TransientError):
        await h.gmail.get_message("m1")


@pytest.mark.parametrize("bad", [{"id": "m1"}, {"id": "m1", "threadId": "t1", "internalDate": []}])
async def test_send_follow_up_parse_failure_is_result_unknown(bad: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "m1", "threadId": "t1"})
        return httpx.Response(200, json={**bad, "payload": "not-a-dict"})

    h = Harness(handler)
    with pytest.raises(ResultUnknown):
        await h.gmail.send_draft("dr-1")


async def test_read_parse_failure_is_transient_without_content() -> None:
    h = Harness(lambda r: httpx.Response(200, json={"id": "m1", "payload": {"headers": "비밀"}}))
    with pytest.raises(TransientError) as info:
        await h.gmail.get_message("m1")
    assert "비밀" not in info.value.message
    h2 = Harness(lambda r: httpx.Response(200, json={"messages": [{"nope": 1}]}))
    with pytest.raises(TransientError):
        await h2.gmail.search_messages("hello")


async def test_get_draft_with_malformed_message_is_transient() -> None:
    h = Harness(lambda r: httpx.Response(200, json={"id": "dr-1", "message": {"id": "m"}}))
    with pytest.raises(TransientError):
        await h.gmail.get_draft("dr-1")


class NoTokens:
    async def access_token(self) -> str:
        return "t"

    async def refresh(self) -> str:
        return "t"


@pytest.mark.parametrize(
    "url",
    [
        "http://gmail.googleapis.com/gmail/v1",
        "https://googleapis.com.evil.example/gmail/v1",
        "https://evilgoogleapis.com/gmail/v1",
        "https://gmail.googleapis.com@evil.example/gmail/v1",
    ],
)
def test_base_url_must_be_googleapis_host(url: str) -> None:
    with pytest.raises(ValueError, match="googleapis"):
        GmailConnector(NoTokens(), base_url=url)


def test_default_and_test_base_url_rules() -> None:
    GmailConnector(NoTokens())
    GmailConnector(NoTokens(), base_url="https://gmail.test/v1", allow_test_base_url=True)
    with pytest.raises(ValueError):
        GmailConnector(NoTokens(), client=httpx.AsyncClient(base_url="https://evil.example"))


# ---------------------------------------------------------------- 파싱 상한


def _nest(depth: int, leaf: dict[str, object]) -> dict[str, object]:
    part = leaf
    for _ in range(depth):
        part = {"mimeType": "multipart/mixed", "parts": [part]}
    return part


def test_mime_depth_limit_truncates_and_marks() -> None:
    leaf: dict[str, object] = {"mimeType": "text/plain", "body": {"data": b64("깊은 본문")}}
    ok = parse_message(message_resource(payload=_nest(MAX_MIME_DEPTH, leaf)))
    assert ok.body_text == "깊은 본문"
    deep = parse_message(message_resource(payload=_nest(MAX_MIME_DEPTH + 1, leaf)))
    assert "깊은 본문" not in deep.body_text and deep.body_text.endswith(TRUNCATED_MARK)


def test_pathological_depth_does_not_overflow_stack() -> None:
    leaf: dict[str, object] = {"mimeType": "text/plain", "body": {"data": b64("x")}}
    msg = parse_message(message_resource(payload=_nest(5000, leaf)))
    assert msg.body_text.endswith(TRUNCATED_MARK)


def test_body_length_limit_truncates_and_marks() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": b64("가" * (MAX_BODY_CHARS + 50))}}
    msg = parse_message(message_resource(payload=payload))
    assert msg.body_text == "가" * MAX_BODY_CHARS + TRUNCATED_MARK
    exact = {"mimeType": "text/plain", "body": {"data": b64("나" * MAX_BODY_CHARS)}}
    assert parse_message(message_resource(payload=exact)).body_text == "나" * MAX_BODY_CHARS


def test_html_body_length_limit() -> None:
    payload = {"mimeType": "text/html", "body": {"data": b64("<p>" + "다" * (MAX_BODY_CHARS * 2))}}
    msg = parse_message(message_resource(payload=payload))
    assert msg.body_text.endswith(TRUNCATED_MARK)
    assert len(msg.body_text) == MAX_BODY_CHARS + len(TRUNCATED_MARK)
