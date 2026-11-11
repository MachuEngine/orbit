"""계정 연결용 모의 Google: 코드 교환(리프레시 토큰·범위), 리프레시, 철회. 값은 모두 합성이다."""

from typing import Any
from urllib.parse import parse_qsl

import httpx

from google_mock import CLIENT_ID, CLIENT_SECRET, CODE, TOKEN_URL, MockGoogle
from orbit.auth.google import pkce_challenge
from orbit.connections.oauth import REQUEST_SCOPES

CONNECT_REDIRECT_URI = "https://orbit.test/api/connections/google/callback"
REVOKE_URL = "https://mock-google.test/revoke"
REFRESH_TOKEN = "test-refresh-token-SYNTH-0001"
ACCESS_PREFIX = "test-access-token-SYNTH-"


class MockGoogleConnect(MockGoogle):
    def __init__(self, clock: Any) -> None:
        super().__init__(clock)
        self.granted_scope = " ".join(REQUEST_SCOPES)
        self.include_refresh = True
        self.refresh_token = REFRESH_TOKEN
        self.expires_in = 3600
        self.refresh_error: str | None = None  # 예: "invalid_grant"
        self.refresh_status = 400
        self.revoke_status = 200
        self.revoke_body: dict[str, str] = {}
        self.revoke_calls: list[str] = []
        self.refresh_requests = 0
        self.issued = 0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if self.network_down:
            return super()._handle(request)
        if url == REVOKE_URL and request.method == "POST":
            form = dict(parse_qsl(request.content.decode()))
            self.revoke_calls.append(form.get("token", ""))
            return httpx.Response(self.revoke_status, json=self.revoke_body)
        if url == TOKEN_URL and request.method == "POST":
            form = dict(parse_qsl(request.content.decode()))
            if form.get("grant_type") == "refresh_token":
                self.refresh_requests += 1
                if self.refresh_error is not None:
                    return httpx.Response(self.refresh_status, json={"error": self.refresh_error})
                if (
                    form.get("refresh_token") != self.refresh_token
                    or form.get("client_id") != CLIENT_ID
                    or form.get("client_secret") != CLIENT_SECRET
                ):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.issued += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"{ACCESS_PREFIX}{self.issued}",
                        "expires_in": self.expires_in,
                    },
                )
            if form.get("redirect_uri") == CONNECT_REDIRECT_URI:
                self.token_requests.append(form)
                ok = (
                    form.get("grant_type") == "authorization_code"
                    and form.get("code") == CODE
                    and form.get("client_id") == CLIENT_ID
                    and form.get("client_secret") == CLIENT_SECRET
                    and pkce_challenge(form.get("code_verifier", "")) == self.challenge
                )
                if not ok:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                body: dict[str, Any] = {
                    "access_token": f"{ACCESS_PREFIX}code",
                    "expires_in": 3600,
                    "scope": self.granted_scope,
                    "token_type": "Bearer",
                    "id_token": self.id_token(),
                }
                if self.include_refresh:
                    body["refresh_token"] = self.refresh_token
                return httpx.Response(200, json=body)
        return super()._handle(request)
