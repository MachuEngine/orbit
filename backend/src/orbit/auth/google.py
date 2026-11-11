"""Google OIDC 로그인 흐름 (ADR-0016 §1). authorization code + PKCE(S256).

- 시작: `state`·`nonce`·`code_verifier`를 만들어 서명한 임시 쿠키 값과 인가 URL을 돌려준다.
  임시 쿠키는 HMAC-SHA256으로 서명하고 10분 뒤 만료된다(만료 시각은 주입 `Clock`).
- 완료: 쿠키 값과 콜백의 `state`를 대조하고, 코드를 교환한 뒤 ID 토큰을 검증한다.
  JWT 서명은 JWKS의 RS256 키로만 확인한다(알고리즘 고정). `exp`는 PyJWT의 실제 시각이 아니라
  주입 `Clock`으로 직접 판정한다. `iss`는 두 표기를 모두 받는다. `aud`는 클라이언트 ID다.
- 실패는 `LoginError(kind)`로만 알린다. 토큰·코드·검증기·응답 본문은 메시지와 로그에 넣지 않는다.
- Google 엔드포인트는 설정값이다. 테스트는 `httpx.MockTransport`를 주입해 모의 서버로 바꾼다.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from orbit.core.clock import Clock

FLOW_COOKIE = "orbit_login"
FLOW_TTL_SECONDS = 600
ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
SCOPE = "openid email profile"
JWKS_TTL = timedelta(hours=1)
JWKS_MIN_REFRESH = timedelta(seconds=60)
HTTP_TIMEOUT_SECONDS = 10.0
_MAX_FIELD = 2048
_MAC_CONTEXT = b"orbit.login.flow.v1."

LoginErrorKind = Literal[
    "denied",  # 사용자가 Google 동의 화면에서 거절
    "expired",  # 임시 쿠키가 없거나 변조됐거나 만료됨
    "state",  # state 불일치
    "exchange",  # 코드 교환 실패
    "id_token",  # 서명·iss·aud·exp 검증 실패
    "nonce",  # nonce 불일치
    "email_unverified",  # 이메일 미인증
    "not_allowed",  # 허용 목록 밖
    "unavailable",  # Google 또는 DB에 닿지 못함
]


class LoginError(Exception):
    """로그인 실패. 메시지에는 종류만 담는다."""

    def __init__(self, kind: LoginErrorKind) -> None:
        super().__init__(kind)
        self.kind: LoginErrorKind = kind


@dataclass(frozen=True)
class GoogleOIDCConfig:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    auth_url: str
    token_url: str
    jwks_url: str
    signing_key: bytes = field(repr=False)


@dataclass(frozen=True)
class LoginFlow:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)


@dataclass(frozen=True)
class VerifiedIdentity:
    sub: str
    email: str
    display_name: str


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pkce_challenge(verifier: str) -> str:
    return _b64e(hashlib.sha256(verifier.encode("ascii")).digest())


def email_allowed(email: str, allowed: list[str]) -> bool:
    """허용 목록이 비어 있으면 아무도 허용하지 않는다."""
    return email.strip().lower() in set(allowed)


class GoogleOIDC:
    def __init__(
        self,
        config: GoogleOIDCConfig,
        *,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._transport = transport
        self._keys: dict[str, Any] = {}
        self._fetched_at: datetime | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 시작

    def begin(self) -> tuple[str, str]:
        """(Google 인가 URL, 서명한 임시 쿠키 값)."""
        flow = LoginFlow(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(48),  # 64자, RFC 7636 범위(43~128)
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._config.client_id,
                "redirect_uri": self._config.redirect_uri,
                "scope": SCOPE,
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": pkce_challenge(flow.code_verifier),
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in self._config.auth_url else "?"
        return f"{self._config.auth_url}{separator}{query}", self._seal(flow)

    def _mac(self, payload: str) -> bytes:
        return hmac.new(
            self._config.signing_key, _MAC_CONTEXT + payload.encode("ascii"), hashlib.sha256
        ).digest()

    def _seal(self, flow: LoginFlow) -> str:
        expires = int(self._clock.now().timestamp()) + FLOW_TTL_SECONDS
        body = json.dumps(
            {"s": flow.state, "n": flow.nonce, "v": flow.code_verifier, "e": expires},
            separators=(",", ":"),
        )
        payload = _b64e(body.encode("utf-8"))
        return f"{payload}.{_b64e(self._mac(payload))}"

    def read_flow_cookie(self, value: str | None) -> LoginFlow:
        """서명과 만료를 확인한다. 어느 하나라도 틀리면 `LoginError("expired")`."""
        invalid = LoginError("expired")
        if not value or len(value) > _MAX_FIELD or value.count(".") != 1 or not value.isascii():
            raise invalid
        payload, signature = value.split(".")
        try:
            if not hmac.compare_digest(_b64d(signature), self._mac(payload)):
                raise invalid
            data = json.loads(_b64d(payload))
        except (ValueError, UnicodeError) as exc:
            raise invalid from exc
        if not isinstance(data, dict):
            raise invalid
        state, nonce, verifier, expires = (data.get(k) for k in ("s", "n", "v", "e"))
        if not (
            isinstance(state, str)
            and isinstance(nonce, str)
            and isinstance(verifier, str)
            and isinstance(expires, int)
            and not isinstance(expires, bool)
        ):
            raise invalid
        if self._clock.now().timestamp() >= expires:
            raise invalid
        return LoginFlow(state=state, nonce=nonce, code_verifier=verifier)

    # ------------------------------------------------------------ 완료

    async def complete(
        self, *, flow: LoginFlow, state: str | None, code: str | None
    ) -> VerifiedIdentity:
        if not state or not hmac.compare_digest(state.encode(), flow.state.encode()):
            raise LoginError("state")
        if not code or len(code) > _MAX_FIELD:
            raise LoginError("exchange")
        id_token = await self._exchange(code, flow.code_verifier)
        return await self._verify(id_token, flow.nonce)

    async def _exchange(self, code: str, code_verifier: str) -> str:
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
        except httpx.HTTPError as exc:
            raise LoginError("unavailable") from exc
        if response.status_code != 200:
            raise LoginError("exchange")
        try:
            body = response.json()
        except ValueError as exc:
            raise LoginError("exchange") from exc
        token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token or len(token) > 16384:
            raise LoginError("exchange")
        return token

    async def _verify(self, token: str, expected_nonce: str) -> VerifiedIdentity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise LoginError("id_token") from exc
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str):
            raise LoginError("id_token")
        key = await self._key_for(kid)
        if key is None:
            raise LoginError("id_token")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._config.client_id,
                options={
                    "require": ["exp", "iss", "aud", "sub"],
                    "verify_exp": False,  # 주입 시계로 아래에서 판정한다
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise LoginError("id_token") from exc
        if claims.get("iss") not in ISSUERS:
            raise LoginError("id_token")
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, int | float):
            raise LoginError("id_token")
        if self._clock.now().timestamp() >= exp:
            raise LoginError("id_token")
        # aud가 여러 개이거나 azp가 있으면 azp가 우리 클라이언트여야 한다(OIDC Core 3.1.3.7).
        aud, azp = claims.get("aud"), claims.get("azp")
        if (isinstance(aud, list) or azp is not None) and azp != self._config.client_id:
            raise LoginError("id_token")
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(
            nonce.encode(), expected_nonce.encode()
        ):
            raise LoginError("nonce")
        verified = claims.get("email_verified")
        if not (verified is True or verified == "true"):  # `1 == True`라서 in 비교를 쓰지 않는다
            raise LoginError("email_unverified")
        sub, email, name = claims.get("sub"), claims.get("email"), claims.get("name")
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise LoginError("id_token")
        if not isinstance(email, str) or not email.strip() or len(email) > 320:
            raise LoginError("id_token")
        display = name.strip() if isinstance(name, str) and name.strip() else email.strip()
        return VerifiedIdentity(sub=sub, email=email.strip(), display_name=display[:200])

    async def _key_for(self, kid: str) -> Any | None:
        async with self._lock:
            now = self._clock.now()
            fresh = self._fetched_at is not None and now - self._fetched_at < JWKS_TTL
            if fresh and kid in self._keys:
                return self._keys[kid]
            # 모르는 kid 때문에 JWKS를 연달아 조회하지 않게 간격을 둔다.
            throttled = (
                fresh and self._fetched_at is not None and now - self._fetched_at < JWKS_MIN_REFRESH
            )
            if not throttled:
                await self._fetch_keys(now)
            return self._keys.get(kid)

    async def _fetch_keys(self, now: datetime) -> None:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=HTTP_TIMEOUT_SECONDS
            ) as client:
                response = await client.get(self._config.jwks_url)
            if response.status_code != 200:
                raise LoginError("unavailable")
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LoginError("unavailable") from exc
        entries = body.get("keys") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise LoginError("unavailable")
        keys: dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("kty") != "RSA":
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or entry.get("alg", "RS256") != "RS256":
                continue
            if entry.get("use", "sig") != "sig":
                continue
            try:
                keys[kid] = RSAAlgorithm.from_jwk(json.dumps(entry))
            except (jwt.PyJWTError, ValueError):
                continue
        self._keys = keys
        self._fetched_at = now
