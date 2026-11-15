"""데스크톱 로그인 핸드오프 (ADR-0024 §4, 마이그레이션 0012). 모의 Google, 합성 사용자만 쓴다."""
# ruff: noqa: F811

import asyncio
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from google_mock import CODE
from integration.test_connections import (  # noqa: F401
    env,
    key_file,
    owner_engine,
)
from integration.test_google_login import ALICE, BOB, Env
from orbit.auth.desktop import purge_expired_handoffs
from orbit.auth.google import pkce_challenge

PORT = 53123
APP_STATE = secrets.token_urlsafe(32)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, pkce_challenge(verifier)


async def _desktop_start(
    env: Env,
    client: httpx.AsyncClient,
    challenge: str,
    *,
    port: int = PORT,
    app_state: str = APP_STATE,
) -> httpx.Response:
    response = await client.get(
        "/api/auth/desktop/start",
        params={"code_challenge": challenge, "port": str(port), "state": app_state},
    )
    assert response.status_code == 302, response.text
    env.mock.observe_authorization(response.headers["location"])
    return response


def _loopback(response: httpx.Response) -> tuple[str, dict[str, list[str]]]:
    assert response.status_code == 302
    parts = urlsplit(response.headers["location"])
    assert (parts.scheme, parts.hostname, parts.path) == ("http", "127.0.0.1", "/callback")
    return f"{parts.scheme}://{parts.netloc}", parse_qs(parts.query)


async def _handoff(
    env: Env, who: dict[str, str], challenge: str, *, port: int = PORT
) -> tuple[str, httpx.Response]:
    """브라우저(쿠키 저장소가 따로인 클라이언트)에서 로그인을 끝내고 (코드, 응답)을 돌려준다."""
    env.set_identity(who)
    browser = env.new_client()
    await _desktop_start(env, browser, challenge, port=port)
    response = await env.callback(browser)
    _, query = _loopback(response)
    assert query["state"] == [APP_STATE]
    return query["code"][0], response


async def _exchange(
    client: httpx.AsyncClient, code: str, verifier: str, **extra: object
) -> httpx.Response:
    return await client.post(
        "/api/auth/desktop/exchange", json={"code": code, "code_verifier": verifier, **extra}
    )


def _cookie_lines(response: httpx.Response) -> dict[str, str]:
    return {c.split("=", 1)[0]: c.lower() for c in response.headers.get_list("set-cookie")}


# ---------------------------------------------------------------- 시작 입력 검증


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"code_challenge": "A" * 43, "port": "53123"},  # state 없음
        {"code_challenge": "A" * 43, "state": APP_STATE},  # port 없음
        {"port": "53123", "state": APP_STATE},  # challenge 없음
        {"code_challenge": "A" * 42, "port": "53123", "state": APP_STATE},
        {"code_challenge": "A" * 44, "port": "53123", "state": APP_STATE},
        {"code_challenge": "A" * 42 + "=", "port": "53123", "state": APP_STATE},
        {"code_challenge": "A" * 42 + "+", "port": "53123", "state": APP_STATE},
        {"code_challenge": "A" * 43, "port": "1023", "state": APP_STATE},
        {"code_challenge": "A" * 43, "port": "80", "state": APP_STATE},
        {"code_challenge": "A" * 43, "port": "65536", "state": APP_STATE},
        {"code_challenge": "A" * 43, "port": "-1", "state": APP_STATE},
        {"code_challenge": "A" * 43, "port": "5312x", "state": APP_STATE},
        {"code_challenge": "A" * 43, "port": "053123", "state": APP_STATE},
        {"code_challenge": "A" * 43, "port": "", "state": APP_STATE},
        {"code_challenge": "A" * 43, "port": "53123", "state": "short"},
        {"code_challenge": "A" * 43, "port": "53123", "state": "a" * 129},
        {"code_challenge": "A" * 43, "port": "53123", "state": "a" * 20 + "&x=1"},
    ],
)
async def test_start_rejects_malformed_input(env: Env, params: dict[str, str]) -> None:
    client = env.new_client()
    response = await client.get("/api/auth/desktop/start", params=params)
    assert (response.status_code, response.json()["detail"]) == (400, "invalid_request")
    assert "orbit_login" not in _cookie_lines(response)


async def test_start_redirects_to_google_with_a_signed_cookie_bound_to_the_request(
    env: Env,
) -> None:
    _, challenge = _pkce()
    client = env.new_client()
    response = await _desktop_start(env, client, challenge)
    cookie = _cookie_lines(response)["orbit_login"]
    for attribute in ("httponly", "samesite=lax", "secure", "max-age=600", "path=/api/auth/google"):
        assert attribute in cookie
    assert response.headers["cache-control"] == "no-store"
    # 앱의 challenge·포트는 Google로 가는 주소가 아니라 서명 쿠키에만 있다.
    location = response.headers["location"]
    assert challenge not in location and str(PORT) not in location and APP_STATE not in location


# ---------------------------------------------------------------- 정상 흐름


async def test_callback_hands_off_a_code_without_creating_a_browser_session(env: Env) -> None:
    verifier, challenge = _pkce()
    code, response = await _handoff(env, ALICE, challenge)
    cookies = _cookie_lines(response)
    assert "orbit_session" not in cookies and "orbit_csrf" not in cookies
    assert "max-age=0" in cookies["orbit_login"]
    assert response.headers["cache-control"] == "no-store"
    assert len(code) >= 43
    assert env.scalar("SELECT count(*) FROM auth_session WHERE user_id IS NOT NULL") == 0
    # 사용자와 워크스페이스는 웹 로그인과 같은 방식으로 만든다.
    assert env.scalar("SELECT count(*) FROM app_user WHERE google_sub = 'test-sub-alice'") == 1
    # 첫 교환 시도 전에는 사용된 흔적이 없다.
    assert env.scalar("SELECT count(*) FROM desktop_login_code WHERE used_at IS NULL") == 1
    assert verifier not in code


async def test_exchange_sets_session_and_csrf_cookies_like_the_web_login(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    app = env.new_client()  # 앱 창의 쿠키 저장소
    response = await _exchange(app, code, verifier)
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"
    cookies = _cookie_lines(response)
    assert "httponly" in cookies["orbit_session"]
    assert "httponly" not in cookies["orbit_csrf"]
    for name in ("orbit_session", "orbit_csrf"):
        assert "secure" in cookies[name]
        assert "samesite=lax" in cookies[name]
        assert "path=/" in cookies[name]
        assert "max-age=" in cookies[name]

    info = (await app.get("/api/session")).json()
    assert info["authenticated"] is True and info["email"] == ALICE["email"]
    csrf = app.cookies.get("orbit_csrf")
    assert csrf
    created = await app.post("/api/runs", json={"message": "안녕"}, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201, created.text
    ws = info["workspace_id"]
    assert (
        env.scalar(
            "SELECT count(*) FROM auth_session WHERE workspace_id = :ws AND user_id IS NOT NULL",
            ws=ws,
        )
        == 1
    )
    assert env.scalar("SELECT count(*) FROM desktop_login_code WHERE used_at IS NOT NULL") == 1


async def test_database_keeps_only_the_hash_of_the_code(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    dump = env.scalar("SELECT string_agg(t::text, ' ') FROM desktop_login_code t")
    assert code not in dump and verifier not in dump
    assert challenge in dump  # PKCE challenge는 공개 값이다
    assert env.scalar("SELECT count(*) FROM desktop_login_code WHERE code_hash ~ '^[0-9a-f]{64}$'")


# ---------------------------------------------------------------- 교환 실패


async def test_expired_code_is_rejected(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    env.clock.advance(timedelta(seconds=61))
    app = env.new_client()
    response = await _exchange(app, code, verifier)
    assert (response.status_code, response.json()["detail"]) == (400, "invalid_grant")
    assert "orbit_session" not in _cookie_lines(response)
    assert env.scalar("SELECT count(*) FROM auth_session WHERE user_id IS NOT NULL") == 0


async def test_code_is_valid_until_the_last_second(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    env.clock.advance(timedelta(seconds=59))
    assert (await _exchange(env.new_client(), code, verifier)).status_code == 204


async def test_reused_code_fails_and_revokes_the_session_it_created(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    app = env.new_client()
    assert (await _exchange(app, code, verifier)).status_code == 204
    assert (await app.get("/api/session")).json()["authenticated"] is True

    again = env.new_client()
    response = await _exchange(again, code, verifier)
    assert (response.status_code, response.json()["detail"]) == (400, "invalid_grant")
    assert "orbit_session" not in _cookie_lines(response)
    # 재사용 시도는 앞서 만든 세션을 무효로 만든다.
    assert (await app.get("/api/session")).json()["authenticated"] is False
    assert env.scalar("SELECT count(*) FROM auth_session WHERE revoked_at IS NULL") == 0


async def test_reuse_with_a_wrong_verifier_does_not_touch_the_session(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    app = env.new_client()
    assert (await _exchange(app, code, verifier)).status_code == 204
    other, _ = _pkce()
    assert (await _exchange(env.new_client(), code, other)).status_code == 400
    assert (await app.get("/api/session")).json()["authenticated"] is True


async def test_wrong_verifier_is_rejected_without_burning_the_code(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    wrong, _ = _pkce()
    bad = await _exchange(env.new_client(), code, wrong)
    assert (bad.status_code, bad.json()["detail"]) == (400, "invalid_grant")
    assert env.scalar("SELECT count(*) FROM auth_session WHERE user_id IS NOT NULL") == 0
    assert (await _exchange(env.new_client(), code, verifier)).status_code == 204


async def test_a_code_is_bound_to_its_user(env: Env) -> None:
    alice_verifier, alice_challenge = _pkce()
    bob_verifier, bob_challenge = _pkce()
    alice_code, _ = await _handoff(env, ALICE, alice_challenge)
    bob_code, _ = await _handoff(env, BOB, bob_challenge, port=PORT + 1)
    # 다른 사용자의 검증기나 코드로는 교환되지 않는다.
    assert (await _exchange(env.new_client(), alice_code, bob_verifier)).status_code == 400
    assert (await _exchange(env.new_client(), bob_code, alice_verifier)).status_code == 400
    assert (await _exchange(env.new_client(), "A" * 43, alice_verifier)).status_code == 400
    alice_app, bob_app = env.new_client(), env.new_client()
    assert (await _exchange(alice_app, alice_code, alice_verifier)).status_code == 204
    assert (await _exchange(bob_app, bob_code, bob_verifier)).status_code == 204
    alice_info = (await alice_app.get("/api/session")).json()
    bob_info = (await bob_app.get("/api/session")).json()
    assert alice_info["email"] == ALICE["email"] and bob_info["email"] == BOB["email"]
    assert alice_info["workspace_id"] != bob_info["workspace_id"]


async def test_all_failures_look_the_same(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    other, _ = _pkce()
    client = env.new_client()
    bodies = [
        await _exchange(client, "unknown-" + "x" * 40, verifier),  # 없는 코드
        await _exchange(client, code, other),  # 틀린 검증기
        await _exchange(client, "짧", verifier),  # 형식 오류
        await _exchange(client, code, "short"),  # 형식 오류
        await client.post("/api/auth/desktop/exchange", content=b"not json"),
        await client.post("/api/auth/desktop/exchange", json=["list"]),
        await client.post("/api/auth/desktop/exchange", json={"code": code}),
        await client.post("/api/auth/desktop/exchange", json={"code": 1, "code_verifier": 2}),
    ]
    for response in bodies:
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid_grant"}
        assert "set-cookie" not in response.headers
    env.clock.advance(timedelta(seconds=61))
    expired = await _exchange(client, code, verifier)  # 만료
    assert (expired.status_code, expired.json()) == (400, {"detail": "invalid_grant"})


async def test_concurrent_exchanges_of_one_code_succeed_once(env: Env) -> None:
    verifier, challenge = _pkce()
    code, _ = await _handoff(env, ALICE, challenge)
    clients = [env.new_client() for _ in range(4)]
    results = await asyncio.gather(*(_exchange(c, code, verifier) for c in clients))
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(204) <= 1  # 동시에 와도 세션은 최대 하나
    # 하나가 성공했다면 나머지는 거절이다. 재사용 폐기 때문에 최종 유효 세션이 0일 수 있다.
    assert set(statuses) <= {204, 400}
    assert env.scalar("SELECT count(*) FROM desktop_login_code WHERE used_at IS NOT NULL") == 1
    assert env.scalar("SELECT count(*) FROM auth_session WHERE user_id IS NOT NULL") <= 1


# ---------------------------------------------------------------- 콜백 실패는 loopback으로 알린다


async def _expect_error(env: Env, response: httpx.Response, kind: str, *, port: int = PORT) -> None:
    base, query = _loopback(response)
    assert base == f"http://127.0.0.1:{port}"
    assert query == {"error": [kind], "state": [APP_STATE]}
    cookies = _cookie_lines(response)
    assert "orbit_session" not in cookies
    assert "max-age=0" in cookies["orbit_login"]
    assert env.scalar("SELECT count(*) FROM desktop_login_code") == 0
    assert env.scalar("SELECT count(*) FROM auth_session WHERE user_id IS NOT NULL") == 0


async def test_email_outside_the_allow_list_gets_no_code(env: Env) -> None:
    env.set_identity({"sub": "test-sub-carol", "email": "carol@example.test", "name": "캐럴"})
    _, challenge = _pkce()
    browser = env.new_client()
    await _desktop_start(env, browser, challenge)
    await _expect_error(env, await env.callback(browser), "not_allowed")
    assert env.scalar("SELECT count(*) FROM app_user WHERE google_sub LIKE 'test-sub-%'") == 0


async def test_state_mismatch_and_denied_consent_report_to_the_app(env: Env) -> None:
    env.set_identity(ALICE)
    _, challenge = _pkce()
    browser = env.new_client()
    await _desktop_start(env, browser, challenge)
    await _expect_error(env, await env.callback(browser, state="forged"), "state")
    await _desktop_start(env, browser, challenge)
    denied = await browser.get(
        "/api/auth/google/callback", params={"error": "access_denied", "state": env.mock.state}
    )
    await _expect_error(env, denied, "denied")


async def test_google_failure_is_reported_to_the_app(env: Env) -> None:
    env.set_identity(ALICE)
    _, challenge = _pkce()
    browser = env.new_client()
    await _desktop_start(env, browser, challenge)
    env.mock.network_down = True
    await _expect_error(env, await env.callback(browser), "unavailable")


async def test_unreadable_flow_cookie_never_redirects_to_loopback(env: Env) -> None:
    env.set_identity(ALICE)
    bare = env.new_client()
    response = await env.callback(bare, state="whatever")
    assert response.status_code == 302
    assert urlsplit(response.headers["location"]).path == "/"  # 웹 방식의 오류 화면
    assert "127.0.0.1" not in response.headers["location"]


async def test_tampered_desktop_binding_is_not_trusted(env: Env) -> None:
    env.set_identity(ALICE)
    _, challenge = _pkce()
    victim = env.new_client()
    await _desktop_start(env, victim, challenge)
    cookie = victim.cookies.get("orbit_login", domain="orbit.test", path="/api/auth/google")
    assert cookie
    payload, signature = cookie.split(".")
    forger = env.new_client()
    response = await forger.get(
        "/api/auth/google/callback",
        params={"code": CODE, "state": env.mock.state},
        headers={"Cookie": f"orbit_login={payload[:-2]}AA.{signature}"},
    )
    assert "127.0.0.1" not in response.headers["location"]
    assert env.scalar("SELECT count(*) FROM desktop_login_code") == 0


# ---------------------------------------------------------------- 웹 흐름 회귀, 정리, 권한


async def test_web_login_still_sets_the_browser_session(env: Env) -> None:
    client = await env.login(ALICE)
    assert (await client.get("/api/session")).json()["authenticated"] is True
    assert env.scalar("SELECT count(*) FROM desktop_login_code") == 0


async def test_expired_rows_are_purged_after_an_hour(env: Env) -> None:
    _, challenge = _pkce()
    await _handoff(env, ALICE, challenge)
    assert await purge_expired_handoffs(env.db, env.clock) == 0
    env.clock.advance(timedelta(seconds=60 + 3600 - 1))
    assert await purge_expired_handoffs(env.db, env.clock) == 0  # 만료 후 1시간 전
    env.clock.advance(timedelta(seconds=2))
    assert await purge_expired_handoffs(env.db, env.clock) == 1
    assert env.scalar("SELECT count(*) FROM desktop_login_code") == 0


async def test_app_role_cannot_read_or_change_the_tables_directly(env: Env) -> None:
    _, challenge = _pkce()
    await _handoff(env, ALICE, challenge)
    ws = env.scalar("SELECT workspace_id FROM desktop_login_code")
    for sql in (
        "SELECT count(*) FROM desktop_login_code",
        "UPDATE desktop_login_code SET used_at = now()",
        "DELETE FROM desktop_login_code",
        "SELECT count(*) FROM desktop_connect_ticket",
    ):
        with pytest.raises(DBAPIError) as info:
            async with env.db.workspace_session(ws) as s:
                await s.execute(text(sql))
        assert getattr(info.value.orig, "sqlstate", None) == "42501", sql
    assert env.scalar("SELECT count(*) FROM desktop_login_code") == 1
