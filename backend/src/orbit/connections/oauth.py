"""Google 계정 연결 OAuth 흐름 (ADR-0016 §2). 로그인(`orbit.auth.google`)과 별도 동의다.

- 시작: Gmail·Calendar 범위를 `access_type=offline`, `prompt=consent`,
  `include_granted_scopes=true`로 요청한다. `state`·`nonce`·PKCE는 로그인과 같은 방식의 서명 임시
  쿠키(`orbit_connect`, 서명 문맥을 로그인과 달리한다)에 둔다.
- 완료: state 대조, 코드 교환, ID 토큰 검증(6A의 `GoogleOIDC._verify` 재사용)으로 연결 계정의
  `sub`·email을 얻는다. 로그인 사용자와 다른 Google 계정이어도 허용한다.
- 토큰·코드·응답 본문은 로그와 예외 메시지에 넣지 않는다. 실패는 `ConnectError(kind)`다.
- 철회: Google 철회 엔드포인트에 토큰을 보내고 결과 종류만 돌려준다.
- 문서 권한 추가(ADR-0020 §1): 선택 범위(`DRIVE_SCOPES`)만 `include_granted_scopes=true`로 요청한다.
  같은 서명 쿠키에 목적(`docs`)과 대상 연결 ID를 담는다. 쿠키 서명이 이 값들을 지켜 준다.
"""

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from orbit.auth.google import (
    FLOW_TTL_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    GoogleOIDC,
    GoogleOIDCConfig,
    LoginError,
    LoginFlow,
    _b64d,
    _b64e,
    pkce_challenge,
)
from orbit.core.clock import Clock

CONNECT_COOKIE = "orbit_connect"
CONNECT_PATH = "/api/connections/google"
_MAC_CONTEXT = b"orbit.connect.flow.v1."

# ADR-0016 §2 최소 집합. Calendar는 일정 읽기·쓰기(calendar.events)와 캘린더 목록 조회
# (calendarlist.readonly), Gmail은 검색·읽기(readonly)와 초안 생성·발송(compose, SPIKE-005).
DATA_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)
REQUEST_SCOPES: tuple[str, ...] = ("openid", "email", *DATA_SCOPES)
# 선택 권한: Drive 파일 읽기(제한 범위, ADR-0020 §1). 첫 연결에는 요청하지 않는다.
DRIVE_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/drive.readonly",)
DOCS_PURPOSE = "docs"

ConnectErrorKind = Literal[
    "denied",
    "expired",
    "state",
    "exchange",
    "id_token",
    "nonce",
    "email_unverified",
    "unavailable",
    "scope_missing",  # 요청한 범위를 모두 허용하지 않음
    "no_refresh_token",
    "already_connected",
    "unauthenticated",
    "docs_scope_missing",  # 문서 권한 추가에서 문서 범위를 허용하지 않음
    "account_mismatch",  # 문서 권한 추가에서 다른 Google 계정을 골랐음
    "connection_inactive",  # 문서 권한을 추가할 활성 연결이 없음
]

RevokeOutcome = Literal["revoked", "failed", "none"]


class ConnectError(Exception):
    def __init__(self, kind: ConnectErrorKind) -> None:
        super().__init__(kind)
        self.kind: ConnectErrorKind = kind


@dataclass(frozen=True)
class ConnectFlow:
    """서명 쿠키에서 읽은 연결 흐름. `purpose`가 `docs`면 `connection_id`의 문서 권한 추가다."""

    flow: LoginFlow
    purpose: str | None = None
    connection_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ConnectGrant:
    """코드 교환 결과. 토큰 원문은 `repr`에 나오지 않는다."""

    sub: str
    email: str
    scopes: tuple[str, ...]
    refresh_token: SecretStr | None = field(default=None, repr=False)
    access_token: SecretStr | None = field(default=None, repr=False)


class GoogleConnectOAuth(GoogleOIDC):
    def __init__(
        self,
        config: GoogleOIDCConfig,
        *,
        revoke_url: str,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(config, clock=clock, transport=transport)
        self._revoke_url = revoke_url

    def _mac(self, payload: str) -> bytes:
        return hmac.new(
            self._config.signing_key, _MAC_CONTEXT + payload.encode("ascii"), hashlib.sha256
        ).digest()

    def _seal_connect(self, flow: LoginFlow, extra: dict[str, str]) -> str:
        expires = int(self._clock.now().timestamp()) + FLOW_TTL_SECONDS
        body = json.dumps(
            {"s": flow.state, "n": flow.nonce, "v": flow.code_verifier, "e": expires, **extra},
            separators=(",", ":"),
        )
        payload = _b64e(body.encode("utf-8"))
        return f"{payload}.{_b64e(self._mac(payload))}"

    def read_connect_flow(self, value: str | None) -> ConnectFlow:
        """서명·만료를 확인하고 목적과 대상 연결을 함께 읽는다. 틀리면 `LoginError("expired")`."""
        flow = self.read_flow_cookie(value)
        assert value is not None  # read_flow_cookie가 None을 거절한다  # noqa: S101
        data = json.loads(_b64d(value.split(".")[0]))
        purpose, raw_id = data.get("p"), data.get("c")
        if purpose is None and raw_id is None:
            return ConnectFlow(flow)
        try:
            if purpose != DOCS_PURPOSE or not isinstance(raw_id, str):
                raise ValueError  # noqa: TRY301
            return ConnectFlow(flow, purpose, uuid.UUID(raw_id))
        except ValueError:
            raise LoginError("expired") from None

    def begin_scope_add(self, connection_id: uuid.UUID, *, login_hint: str) -> tuple[str, str]:
        """문서 권한 추가: (Google 인가 URL, 서명한 임시 쿠키 값).

        선택 범위만 요청하고 이미 허용한 범위는 `include_granted_scopes`로 합친다.
        `login_hint`는 같은 계정을 고르도록 돕는 값일 뿐이고, 계정 일치는 콜백이 `sub`로 확인한다.
        """
        flow = LoginFlow(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(48),
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._config.client_id,
                "redirect_uri": self._config.redirect_uri,
                "scope": " ".join(("openid", "email", *DRIVE_SCOPES)),
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "login_hint": login_hint,
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": pkce_challenge(flow.code_verifier),
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in self._config.auth_url else "?"
        cookie = self._seal_connect(flow, {"p": DOCS_PURPOSE, "c": str(connection_id)})
        return f"{self._config.auth_url}{separator}{query}", cookie

    def begin_connect(self) -> tuple[str, str]:
        """(Google 인가 URL, 서명한 임시 쿠키 값)."""
        flow = LoginFlow(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(48),
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._config.client_id,
                "redirect_uri": self._config.redirect_uri,
                "scope": " ".join(REQUEST_SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": pkce_challenge(flow.code_verifier),
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in self._config.auth_url else "?"
        return f"{self._config.auth_url}{separator}{query}", self._seal(flow)

    async def complete_connect(
        self, *, flow: LoginFlow, state: str | None, code: str | None
    ) -> ConnectGrant:
        if not state or not hmac.compare_digest(state.encode(), flow.state.encode()):
            raise ConnectError("state")
        if not code or len(code) > 2048:
            raise ConnectError("exchange")
        body = await self._exchange_body(code, flow.code_verifier)
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token or len(id_token) > 16384:
            raise ConnectError("exchange")
        try:
            identity = await self._verify(id_token, flow.nonce)
        except LoginError as exc:
            kind = exc.kind
            if kind in ("id_token", "nonce", "email_unverified", "unavailable"):
                raise ConnectError(kind) from None
            raise ConnectError("id_token") from None
        scope = body.get("scope")
        scopes = tuple(sorted(set(scope.split()))) if isinstance(scope, str) else ()
        return ConnectGrant(
            sub=identity.sub,
            email=identity.email,
            scopes=scopes,
            refresh_token=_secret(body.get("refresh_token")),
            access_token=_secret(body.get("access_token")),
        )

    async def _exchange_body(self, code: str, code_verifier: str) -> dict[str, object]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._config.redirect_uri,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "code_verifier": code_verifier,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=HTTP_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(self._config.token_url, data=form)
        except httpx.HTTPError:
            raise ConnectError("unavailable") from None
        if response.status_code != 200:
            raise ConnectError("exchange")
        try:
            body = response.json()
        except ValueError:
            raise ConnectError("exchange") from None
        if not isinstance(body, dict):
            raise ConnectError("exchange")
        return body

    async def revoke(self, token: str) -> RevokeOutcome:
        """Google에 토큰 철회를 요청한다. 이미 무효인 토큰(400 invalid_token)도 철회로 본다."""
        return await revoke_token(self._revoke_url, token, transport=self._transport)


def _secret(value: object) -> SecretStr | None:
    return SecretStr(value) if isinstance(value, str) and value else None


def missing_scopes(granted: tuple[str, ...]) -> list[str]:
    have = set(granted)
    return [s for s in DATA_SCOPES if s not in have]


def has_docs_scope(granted: tuple[str, ...] | list[str]) -> bool:
    return all(s in set(granted) for s in DRIVE_SCOPES)


async def revoke_token(
    revoke_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> RevokeOutcome:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(revoke_url, data={"token": token})
    except httpx.HTTPError:
        return "failed"
    if response.status_code == 200:
        return "revoked"
    if response.status_code == 400:
        try:
            body = response.json()
        except ValueError:
            return "failed"
        if isinstance(body, dict) and body.get("error") == "invalid_token":
            return "revoked"
    return "failed"
