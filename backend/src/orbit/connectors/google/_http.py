"""Google 어댑터(Calendar·Gmail) 공통 HTTP 처리 (ADR-0016 4절, ADR-0003).

여기에 모은 규칙은 두 어댑터가 같게 동작해야 하는 부분이다.
- `base_url` 검증: 액세스 토큰을 실어 보내는 곳이므로 `https://*.googleapis.com`만 허용한다.
  테스트만 `allow_test_base_url=True`로 풀 수 있고 `client` 주입도 그때만 가능하다.
- 토큰: 요청마다 공급자에게서 받는다. 401이면 `refresh()` 후 같은 요청을 한 번만 다시 보낸다
  (401은 적용되지 않았다는 뜻이라 쓰기도 안전하다). 갱신 실패와 갱신 후 401은 `PermissionRevoked`.
- 읽기만 자동 재시도한다(5xx, 전송 오류, 짧은 `Retry-After`의 429).
- 쓰기는 재시도하지 않는다. 요청이 나간 뒤의 타임아웃·연결 끊김·`httpx.RequestError` 전부·408·5xx·
  읽을 수 없는 성공 응답은 `ResultUnknown`이다. 연결 전 실패(연결 실패·풀 대기 초과·토큰 공급
  실패)는 `TransientError`다.
- 오류 대응: `invalid_grant`·`insufficientPermissions`·`authError`와 갱신 후 401만
  `PermissionRevoked`. 그 밖의 403은 사유 코드를 담은 일반 `ConnectorError`다(권한 철회로 오해해
  재연결을 요구하지 않게 한다). 429와 403 `rateLimitExceeded`·`userRateLimitExceeded`는
  `RateLimited`.
- 오류 메시지에는 서비스·동작 이름, HTTP 상태, 길이를 제한한 사유 코드만 넣는다. 응답 본문 문장·
  내용·토큰은 넣지 않는다.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from orbit.connectors.contracts import (
    AlreadyExists,
    ConnectorError,
    InvalidQuery,
    NotFound,
    PermissionRevoked,
    PreconditionFailed,
    RateLimited,
    ResultUnknown,
    TransientError,
)
from orbit.connectors.google.tokens import AccessTokenProvider

Sleep = Callable[[float], Awaitable[None]]

_RATE_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded"})
_PERMISSION_REASONS = frozenset({"insufficientpermissions", "autherror"})
_REASON_SAFE = re.compile(r"[A-Za-z0-9_]{1,40}")
_MAX_AUTO_RETRY_AFTER_S = 2.0


def check_base_url(base_url: str) -> None:
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host.endswith(".googleapis.com"):
        raise ValueError("base_url은 https://*.googleapis.com 이어야 한다")


def error_reason(resp: httpx.Response) -> str:
    """Google 오류 본문의 사유 코드만 꺼낸다. 메시지 문장은 내용을 담을 수 있어 쓰지 않는다."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, str):  # OAuth 형식: {"error": "invalid_grant"}
        return err
    if isinstance(err, dict):
        message = err.get("message")
        if isinstance(message, str) and "invalid_grant" in message.lower():
            return "invalid_grant"
        errors = err.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("reason", ""))
        return str(err.get("status", ""))
    return ""


def safe_reason(reason: str) -> str:
    """사유 코드는 영숫자·밑줄 40자까지만 오류 메시지에 싣는다."""
    return reason if _REASON_SAFE.fullmatch(reason) else "unknown"


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def http_error(
    resp: httpx.Response,
    *,
    service: str,
    op: str,
    write: bool,
    creating: bool = False,
    query: bool = False,
) -> ConnectorError:
    status = resp.status_code
    reason = error_reason(resp)
    key = reason.casefold()
    safe = safe_reason(reason) if reason else ""
    note = f"{service} {op} 실패: HTTP {status}, reason={safe_reason(reason)}"
    if key == "invalid_grant" or status == 401:
        return PermissionRevoked(note, reason=safe)
    if status == 429 or (status == 403 and key in _RATE_REASONS):
        return RateLimited(retry_after_s=_retry_after(resp), message=note, reason=safe)
    if status == 403 and key in _PERMISSION_REASONS:
        return PermissionRevoked(note, reason=safe)
    if status in (404, 410):
        return NotFound(note, reason=safe)
    if status == 409 and creating:
        return AlreadyExists(note, reason=safe)
    if status == 412:
        return PreconditionFailed(note, reason=safe)
    if status == 400 and query:
        return InvalidQuery(note, reason=safe)
    if status == 408 and write:
        return ResultUnknown(note, reason=safe)
    if status >= 500:
        return ResultUnknown(note, reason=safe) if write else TransientError(note, reason=safe)
    return ConnectorError(note, reason=safe)


class GoogleHttp:
    """어댑터 하나가 쓰는 HTTP 클라이언트와 요청 규칙."""

    def __init__(
        self,
        tokens: AccessTokenProvider,
        *,
        service: str,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        allow_test_base_url: bool = False,
        timeout_s: float = 15.0,
        read_retries: int = 2,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not allow_test_base_url:
            if client is not None:
                raise ValueError("client 주입은 allow_test_base_url=True일 때만 쓸 수 있다")
            check_base_url(base_url)
        self._tokens = tokens
        self._service = service
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._owns_client = client is None
        self._read_retries = read_retries
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _token(self, *, refresh: bool) -> str:
        try:
            return await (self._tokens.refresh() if refresh else self._tokens.access_token())
        except PermissionRevoked:
            raise
        except Exception:
            if refresh:
                raise PermissionRevoked("토큰을 새로 받지 못했다") from None
            raise TransientError("액세스 토큰을 받지 못했다") from None

    def _transport_error(self, exc: httpx.RequestError, *, op: str, write: bool) -> ConnectorError:
        before_send = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout)
        kind = type(exc).__name__
        if write and not before_send:
            return ResultUnknown(f"{self._service} {op} 요청 뒤 연결 오류({kind}), 결과를 모른다")
        return TransientError(f"{self._service} {op} 연결 오류({kind})")

    @staticmethod
    def _auto_retry_delay(err: ConnectorError, attempt: int) -> float | None:
        if isinstance(err, TransientError):
            return 0.2 * 2.0**attempt
        if isinstance(err, RateLimited):
            wait: float = err.retry_after_s if err.retry_after_s is not None else 0.5
            return wait if wait <= _MAX_AUTO_RETRY_AFTER_S else None
        return None

    async def request(
        self,
        method: str,
        path: str,
        *,
        op: str,
        write: bool,
        params: Mapping[str, Any] | Sequence[tuple[str, str]] | None = None,
        body: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        creating: bool = False,
        query: bool = False,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """요청을 보내고 JSON 객체를 돌려준다. `allow_empty`이면 빈 성공 응답(204)은 `{}`다."""
        token = await self._token(refresh=False)
        refreshed = False
        attempt = 0
        query_params = (
            params if isinstance(params, Mapping | None) else httpx.QueryParams(tuple(params))
        )
        while True:
            headers = {"Authorization": f"Bearer {token}", **(extra_headers or {})}
            try:
                resp = await self._client.request(
                    method,
                    path,
                    params=query_params,
                    json=body,
                    headers=headers,
                )
            except httpx.RequestError as exc:  # TransportError와 DecodingError 등 모두
                err = self._transport_error(exc, op=op, write=write)
                if not write and attempt < self._read_retries:
                    await self._sleep(0.2 * 2.0**attempt)
                    attempt += 1
                    continue
                raise err from None
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                token = await self._token(refresh=True)
                continue
            if resp.is_success:
                if allow_empty and not resp.content:
                    return {}
                try:
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise ValueError  # noqa: TRY301
                except ValueError:
                    note = f"{self._service} {op} 응답을 읽을 수 없다"
                    raise (ResultUnknown(note) if write else TransientError(note)) from None
                return data
            err = http_error(
                resp, service=self._service, op=op, write=write, creating=creating, query=query
            )
            if not write and attempt < self._read_retries:
                delay = self._auto_retry_delay(err, attempt)
                if delay is not None:
                    await self._sleep(delay)
                    attempt += 1
                    continue
            raise err
