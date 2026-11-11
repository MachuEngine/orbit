"""모의 Google(인가·토큰·JWKS 엔드포인트)과 테스트용 RSA 키. 실제 Google을 호출하지 않는다.

모든 값은 합성이다. 키는 테스트 프로세스 안에서만 만든다.
"""

import json
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from orbit.auth.google import pkce_challenge
from orbit.core.clock import Clock

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-value"
REDIRECT_URI = "https://orbit.test/api/auth/google/callback"
AUTH_URL = "https://mock-google.test/o/oauth2/v2/auth"
TOKEN_URL = "https://mock-google.test/token"
JWKS_URL = "https://mock-google.test/certs"
GOOD_KID = "test-kid-1"
SIGNING_KEY = "test-signing-key-0123456789-abcdefghijklmnop"
CODE = "test-auth-code"


@lru_cache(maxsize=1)
def _keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    good = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return good, attacker


def good_key() -> rsa.RSAPrivateKey:
    return _keys()[0]


def attacker_key() -> rsa.RSAPrivateKey:
    return _keys()[1]


def jwk_for(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


class MockGoogle:
    """인가 요청을 관찰하고, 토큰·JWKS 요청에 응답하는 모의 서버."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.nonce = ""
        self.challenge = ""
        self.state = ""
        self.claims_override: dict[str, Any] = {}
        self.claims_drop: set[str] = set()
        self.sign_key: rsa.RSAPrivateKey = good_key()
        self.token_kid = GOOD_KID
        self.published: list[dict[str, Any]] = [jwk_for(good_key(), GOOD_KID)]
        self.token_status = 200
        self.network_down = False
        self.token_requests: list[dict[str, str]] = []
        self.jwks_requests = 0

    # ------------------------------------------------------------ 테스트가 쓰는 도구

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def observe_authorization(self, location: str) -> dict[str, str]:
        """시작 응답의 Location을 검사하고 이후 토큰 응답에 쓸 값을 기억한다."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTH_URL
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.nonce = params["nonce"]
        self.challenge = params["code_challenge"]
        self.state = params["state"]
        return params

    def claims(self) -> dict[str, Any]:
        now = int(self.clock.now().timestamp())
        base: dict[str, Any] = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "test-sub-alice",
            "email": "alice@example.test",
            "email_verified": True,
            "name": "앨리스 테스트",
            "nonce": self.nonce,
            "iat": now,
            "exp": now + 3600,
        }
        base.update(self.claims_override)
        for name in self.claims_drop:
            base.pop(name, None)
        return base

    def id_token(self, claims: dict[str, Any] | None = None) -> str:
        return jwt.encode(
            claims if claims is not None else self.claims(),
            self.sign_key,
            algorithm="RS256",
            headers={"kid": self.token_kid},
        )

    def edit(self, **overrides: Any) -> None:
        self.claims_override.update(overrides)

    # ------------------------------------------------------------ 모의 서버

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.network_down:
            raise httpx.ConnectError("mock network down", request=request)
        url = str(request.url)
        if url == JWKS_URL and request.method == "GET":
            self.jwks_requests += 1
            return httpx.Response(200, json={"keys": self.published})
        if url == TOKEN_URL and request.method == "POST":
            form = dict(parse_qsl(request.content.decode()))
            self.token_requests.append(form)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            ok = (
                form.get("grant_type") == "authorization_code"
                and form.get("code") == CODE
                and form.get("client_id") == CLIENT_ID
                and form.get("client_secret") == CLIENT_SECRET
                and form.get("redirect_uri") == REDIRECT_URI
                and pkce_challenge(form.get("code_verifier", "")) == self.challenge
            )
            if not ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "unused", "id_token": self.id_token()})
        return httpx.Response(404)
