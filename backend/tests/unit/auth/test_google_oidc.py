"""Google OIDC 흐름: 인가 URL, 임시 쿠키, 코드 교환, ID 토큰 검증. 모의 Google만 쓴다."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from google_mock import (
    AUTH_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    CODE,
    GOOD_KID,
    JWKS_URL,
    REDIRECT_URI,
    SIGNING_KEY,
    TOKEN_URL,
    MockGoogle,
    attacker_key,
    good_key,
    jwk_for,
)
from orbit.auth.google import (
    FLOW_TTL_SECONDS,
    GoogleOIDC,
    GoogleOIDCConfig,
    LoginError,
    LoginFlow,
    email_allowed,
    pkce_challenge,
)
from orbit.core.clock import FixedClock

NOW = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)


def _config(signing_key: bytes) -> GoogleOIDCConfig:
    return GoogleOIDCConfig(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        auth_url=AUTH_URL,
        token_url=TOKEN_URL,
        jwks_url=JWKS_URL,
        signing_key=signing_key,
    )


def make() -> tuple[GoogleOIDC, MockGoogle, FixedClock]:
    clock = FixedClock(NOW)
    mock = MockGoogle(clock)
    return (
        GoogleOIDC(_config(SIGNING_KEY.encode()), clock=clock, transport=mock.transport()),
        (mock),
        clock,
    )


def begin(oidc: GoogleOIDC, mock: MockGoogle) -> tuple[LoginFlow, dict[str, str]]:
    url, cookie = oidc.begin()
    params = mock.observe_authorization(url)
    return oidc.read_flow_cookie(cookie), params


async def kind_of(oidc: GoogleOIDC, flow: LoginFlow, state: str | None, code: str | None) -> str:
    with pytest.raises(LoginError) as info:
        await oidc.complete(flow=flow, state=state, code=code)
    return info.value.kind


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ---------------------------------------------------------------- 인가 URL


def test_authorization_url_has_pkce_state_nonce_and_minimal_scope() -> None:
    oidc, mock, _ = make()
    url, cookie = oidc.begin()
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTH_URL
    params = dict(parse_qsl(parts.query))
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT_URI
    assert params["scope"] == "openid email profile"
    assert params["code_challenge_method"] == "S256"
    flow = oidc.read_flow_cookie(cookie)
    assert params["state"] == flow.state
    assert params["nonce"] == flow.nonce
    assert params["code_challenge"] == pkce_challenge(flow.code_verifier)
    assert 43 <= len(flow.code_verifier) <= 128
    assert "client_secret" not in url
    assert flow.code_verifier not in url
    assert mock.token_requests == []


def test_each_start_uses_fresh_random_values() -> None:
    oidc, _, _ = make()
    a = oidc.read_flow_cookie(oidc.begin()[1])
    b = oidc.read_flow_cookie(oidc.begin()[1])
    assert (a.state, a.nonce, a.code_verifier) != (b.state, b.nonce, b.code_verifier)


def test_pkce_challenge_matches_rfc7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


# ---------------------------------------------------------------- 임시 쿠키


def test_flow_cookie_expires_after_ten_minutes_by_injected_clock() -> None:
    oidc, _, clock = make()
    _, cookie = oidc.begin()
    clock.advance(timedelta(seconds=FLOW_TTL_SECONDS - 1))
    oidc.read_flow_cookie(cookie)
    clock.advance(timedelta(seconds=1))
    with pytest.raises(LoginError) as info:
        oidc.read_flow_cookie(cookie)
    assert info.value.kind == "expired"


def _mac(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, b"orbit.login.flow.v1." + payload.encode(), hashlib.sha256).digest())


def test_flow_cookie_rejects_tampering() -> None:
    oidc, _, _ = make()
    _, cookie = oidc.begin()
    payload, signature = cookie.split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=="))
    body["s"] = "attacker-state"
    forged_payload = _b64(json.dumps(body).encode())
    flipped = signature[:-2] + ("AA" if not signature.endswith("AA") else "BB")
    bad_cookies = [
        None,
        "",
        "garbage",
        "a.b.c",
        f"{forged_payload}.{signature}",  # 본문만 바꿈
        f"{payload}.{flipped}",  # 서명만 바꿈
        f"{payload}.{_mac(b'x' * 32, payload)}",  # 다른 키로 서명
        f"{payload}.",
        f".{signature}",
        "한글.값",
    ]
    for bad in bad_cookies:
        with pytest.raises(LoginError) as info:
            oidc.read_flow_cookie(bad)
        assert info.value.kind == "expired", bad


def test_flow_cookie_signed_with_other_key_is_rejected() -> None:
    oidc, _, clock = make()
    other = GoogleOIDC(_config(b"another-signing-key-0123456789-abcdef"), clock=clock)
    _, cookie = other.begin()
    with pytest.raises(LoginError):
        oidc.read_flow_cookie(cookie)


def test_flow_cookie_with_wrong_field_types_is_rejected() -> None:
    oidc, _, _ = make()
    for body in ({"s": 1, "n": "n", "v": "v", "e": 9_999_999_999}, {"s": "s"}, ["list"]):
        payload = _b64(json.dumps(body).encode())
        cookie = f"{payload}.{_mac(SIGNING_KEY.encode(), payload)}"  # 서명은 올바르다
        with pytest.raises(LoginError):
            oidc.read_flow_cookie(cookie)


# ---------------------------------------------------------------- 정상 흐름


async def test_complete_exchanges_code_with_pkce_and_returns_identity() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    identity = await oidc.complete(flow=flow, state=flow.state, code=CODE)
    assert identity.sub == "test-sub-alice"
    assert identity.email == "alice@example.test"
    assert identity.display_name == "앨리스 테스트"
    form = mock.token_requests[0]
    assert form["code_verifier"] == flow.code_verifier
    assert form["redirect_uri"] == REDIRECT_URI


async def test_display_name_falls_back_to_email() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    mock.claims_drop.add("name")
    identity = await oidc.complete(flow=flow, state=flow.state, code=CODE)
    assert identity.display_name == "alice@example.test"


async def test_both_issuer_forms_and_string_email_verified_are_accepted() -> None:
    for iss in ("accounts.google.com", "https://accounts.google.com"):
        oidc, mock, _ = make()
        flow, _ = begin(oidc, mock)
        mock.edit(iss=iss, email_verified="true")
        assert (await oidc.complete(flow=flow, state=flow.state, code=CODE)).sub


# ---------------------------------------------------------------- 실패 종류


async def test_state_mismatch_or_missing() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    assert await kind_of(oidc, flow, "other-state", CODE) == "state"
    assert await kind_of(oidc, flow, None, CODE) == "state"
    assert await kind_of(oidc, flow, "", CODE) == "state"
    assert mock.token_requests == []  # state가 틀리면 코드를 교환하지 않는다


async def test_missing_or_rejected_code() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    assert await kind_of(oidc, flow, flow.state, None) == "exchange"
    assert await kind_of(oidc, flow, flow.state, "x" * 5000) == "exchange"
    assert await kind_of(oidc, flow, flow.state, "wrong-code") == "exchange"
    mock.token_status = 400
    assert await kind_of(oidc, flow, flow.state, CODE) == "exchange"


async def test_nonce_mismatch_and_missing() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    mock.edit(nonce="someone-elses-nonce")
    assert await kind_of(oidc, flow, flow.state, CODE) == "nonce"
    mock.claims_override.clear()
    mock.claims_drop.add("nonce")
    assert await kind_of(oidc, flow, flow.state, CODE) == "nonce"


async def test_expired_id_token_is_judged_by_injected_clock() -> None:
    oidc, mock, clock = make()
    flow, _ = begin(oidc, mock)
    now = int(clock.now().timestamp())
    mock.edit(exp=now - 1)
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"
    mock.edit(exp=now)  # 정각도 만료다
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"
    mock.edit(exp=now + 1)
    assert (await oidc.complete(flow=flow, state=flow.state, code=CODE)).sub


async def test_token_valid_by_real_clock_is_rejected_when_injected_clock_is_later() -> None:
    clock = FixedClock(datetime(2101, 1, 1, tzinfo=UTC))
    mock = MockGoogle(clock)
    oidc = GoogleOIDC(_config(SIGNING_KEY.encode()), clock=clock, transport=mock.transport())
    flow, _ = begin(oidc, mock)
    # 실제 시각으로는 아직 유효하지만 주입 시계로는 만료된 토큰
    mock.edit(exp=int(datetime(2100, 6, 1, tzinfo=UTC).timestamp()))
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"


async def test_audience_mismatch() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    mock.edit(aud="another-client.apps.googleusercontent.com")
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"
    mock.edit(aud=[CLIENT_ID, "another"])  # 목록이면 azp도 있어야 한다
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"
    mock.edit(azp=CLIENT_ID)
    assert (await oidc.complete(flow=flow, state=flow.state, code=CODE)).sub


async def test_authorized_party_must_be_our_client() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    mock.edit(azp="another-client.apps.googleusercontent.com")  # aud가 단일이어도 azp가 있으면 확인
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"
    mock.edit(aud=[CLIENT_ID, "another"], azp="another")
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"
    mock.edit(aud=CLIENT_ID, azp=CLIENT_ID)
    assert (await oidc.complete(flow=flow, state=flow.state, code=CODE)).sub


async def test_issuer_mismatch() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    for iss in ("https://evil.example", "http://accounts.google.com", ""):
        mock.edit(iss=iss)
        assert await kind_of(oidc, flow, flow.state, CODE) == "id_token", iss
    mock.claims_override.clear()
    mock.claims_drop.add("iss")
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"


async def test_forged_signature_is_rejected() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    mock.sign_key = attacker_key()  # kid는 정상 키와 같지만 서명한 키가 다르다
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"


async def test_unknown_kid_is_rejected() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    mock.token_kid = "no-such-kid"
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"


async def test_unsigned_hmac_confused_and_malformed_tokens_are_rejected() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    claims = mock.claims()
    unsigned = jwt.encode(claims, key="", algorithm="none", headers={"kid": GOOD_KID})
    # HS256 혼동 공격: 공개 키 PEM을 HMAC 비밀로 쓴 토큰
    public_pem = (
        good_key().public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    header = _b64(json.dumps({"alg": "HS256", "kid": GOOD_KID, "typ": "JWT"}).encode())
    signing_input = f"{header}.{_b64(json.dumps(claims).encode())}"
    mac = _b64(hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest())
    hs256 = f"{signing_input}.{mac}"
    for token in (unsigned, hs256, "not.a.jwt", "", "a.b"):
        with pytest.raises(LoginError) as info:
            await oidc._verify(token, flow.nonce)
        assert info.value.kind == "id_token", token[:20]


async def test_email_must_be_verified() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    for value in (False, "false", None, 1, "True"):
        mock.edit(email_verified=value)
        assert await kind_of(oidc, flow, flow.state, CODE) == "email_unverified", value
    mock.claims_override.clear()
    mock.claims_drop.add("email_verified")
    assert await kind_of(oidc, flow, flow.state, CODE) == "email_unverified"


async def test_missing_identity_claims_are_rejected() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    for claim in ("sub", "email"):
        mock.claims_drop = {claim}
        assert await kind_of(oidc, flow, flow.state, CODE) == "id_token", claim


async def test_network_failure_is_unavailable() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    mock.network_down = True
    assert await kind_of(oidc, flow, flow.state, CODE) == "unavailable"


# ---------------------------------------------------------------- JWKS 캐시


async def test_jwks_is_cached_then_refreshed_after_ttl() -> None:
    oidc, mock, clock = make()
    for _ in range(3):
        flow, _ = begin(oidc, mock)
        await oidc.complete(flow=flow, state=flow.state, code=CODE)
    assert mock.jwks_requests == 1
    clock.advance(timedelta(hours=1))
    flow, _ = begin(oidc, mock)
    await oidc.complete(flow=flow, state=flow.state, code=CODE)
    assert mock.jwks_requests == 2


async def test_key_rotation_refetches_jwks_but_not_more_than_once_a_minute() -> None:
    oidc, mock, clock = make()
    flow, _ = begin(oidc, mock)
    await oidc.complete(flow=flow, state=flow.state, code=CODE)
    assert mock.jwks_requests == 1

    mock.sign_key = attacker_key()
    mock.token_kid = "rotated-kid"
    # 아직 게시되지 않은 키: 1분 안에는 다시 조회하지 않고 거절한다.
    flow, _ = begin(oidc, mock)
    clock.advance(timedelta(seconds=30))
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"
    assert mock.jwks_requests == 1
    # 키가 게시되고 1분이 지나면 다시 조회해 통과한다.
    mock.published = [jwk_for(good_key(), GOOD_KID), jwk_for(attacker_key(), "rotated-kid")]
    clock.advance(timedelta(seconds=31))
    flow, _ = begin(oidc, mock)
    assert (await oidc.complete(flow=flow, state=flow.state, code=CODE)).sub
    assert mock.jwks_requests == 2


async def test_empty_jwks_rejects_token() -> None:
    oidc, mock, _ = make()
    flow, _ = begin(oidc, mock)
    mock.published = []
    assert await kind_of(oidc, flow, flow.state, CODE) == "id_token"


# ---------------------------------------------------------------- 허용 목록


def test_empty_allow_list_allows_nobody() -> None:
    assert email_allowed("alice@example.test", []) is False
    assert email_allowed("alice@example.test", ["alice@example.test"]) is True
    assert email_allowed(" ALICE@Example.test ", ["alice@example.test"]) is True
    assert email_allowed("bob@example.test", ["alice@example.test"]) is False
    assert email_allowed("", ["alice@example.test"]) is False
