"""데스크톱 계정 연결: 티켓, 안내 화면, 대기 행, 앱의 완료 (ADR-0024 §5, 마이그레이션 0012).

모의 Google, 합성 데이터만 쓴다. 연결 CSRF(공격자의 티켓 URL을 피해자가 여는 경우) 방지가 핵심이다.
"""
# ruff: noqa: F811

import asyncio
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from connect_mock import CONNECT_REDIRECT_URI
from google_mock import CODE
from integration.test_connections import (  # noqa: F401
    DRIVE,
    _connect,
    _connected,
    _csrf,
    _grant_docs,
    _rows,
    env,
    key_file,
    owner_engine,
)
from integration.test_google_login import ALICE, BOB, Env
from orbit.auth.desktop import purge_expired_handoffs
from orbit.auth.google import pkce_challenge
from orbit.connections.oauth import REQUEST_SCOPES

TICKET_PATH = "/api/connections/google/desktop-ticket"
BEGIN_PATH = "/api/connections/google/desktop-begin"
COMPLETE_PATH = "/api/connections/google/desktop-complete"
PORT = 53124
APP_STATE = secrets.token_urlsafe(32)
INVALID_GRANT = {"detail": "invalid_grant"}


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, pkce_challenge(verifier)


def _app_fields(challenge: str, **override: Any) -> dict[str, Any]:
    return {"code_challenge": challenge, "port": PORT, "app_state": APP_STATE, **override}


async def _ticket(
    client: httpx.AsyncClient,
    challenge: str,
    intent: str = "connect",
    connection_id: str | None = None,
    **override: Any,
) -> httpx.Response:
    body: dict[str, Any] = {"intent": intent, **_app_fields(challenge, **override)}
    if connection_id is not None:
        body["connection_id"] = connection_id
    return await client.post(TICKET_PATH, json=body, headers=_csrf(client))


def _begin_url(response: httpx.Response) -> str:
    assert response.status_code == 200, response.text
    url: str = response.json()["url"]
    assert url.startswith(f"https://orbit.test{BEGIN_PATH}?ticket="), url
    return url.removeprefix("https://orbit.test")


def _form(page: httpx.Response) -> dict[str, str]:
    assert page.status_code == 200, page.text
    return {
        name: re.search(rf'name="{name}" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        for name in ("ticket", "token")
    }


async def _consent(
    env: Env, browser: httpx.AsyncClient, ticket_response: httpx.Response
) -> httpx.Response:
    """시스템 브라우저에서 안내 화면을 열고 "계속"을 눌러 Google로 넘어간다."""
    page = await browser.get(_begin_url(ticket_response))
    posted = await browser.post(BEGIN_PATH, data=_form(page))
    assert posted.status_code == 303, posted.text
    env.mock.observe_authorization(posted.headers["location"])
    return posted


async def _google_callback(browser: httpx.AsyncClient, env: Env, **params: str) -> httpx.Response:
    query = {"code": CODE, "state": env.mock.state, **params}
    return await browser.get("/api/connections/google/callback", params=query)


def _loopback(response: httpx.Response) -> dict[str, list[str]]:
    assert response.status_code == 302, response.text
    parts = urlsplit(response.headers["location"])
    assert (parts.scheme, parts.hostname, parts.port, parts.path) == (
        "http",
        "127.0.0.1",
        PORT,
        "/callback",
    )
    assert response.headers["cache-control"] == "no-store"
    return parse_qs(parts.query)


async def _handoff(env: Env, ticket_response: httpx.Response) -> tuple[httpx.AsyncClient, str]:
    """브라우저 동의까지 마치고 (브라우저, 완료 코드)를 돌려준다."""
    browser = env.new_client()
    await _consent(env, browser, ticket_response)
    query = _loopback(await _google_callback(browser, env))
    assert query["state"] == [APP_STATE]
    return browser, query["code"][0]


async def _complete(
    client: httpx.AsyncClient, code: str, verifier: str, **extra: Any
) -> httpx.Response:
    return await client.post(
        COMPLETE_PATH,
        json={"code": code, "code_verifier": verifier, **extra},
        headers=_csrf(client),
    )


async def _connect_via_app(
    env: Env, client: httpx.AsyncClient, intent: str = "connect", cid: str | None = None
) -> tuple[httpx.Response, str]:
    verifier, challenge = _pkce()
    _, code = await _handoff(env, await _ticket(client, challenge, intent, cid))
    return await _complete(client, code, verifier), code


def _exchanges(env: Env) -> int:
    """Google 토큰 엔드포인트로 간 연결용(connect redirect URI) 코드 교환 횟수."""
    return sum(1 for r in env.mock.token_requests if r.get("redirect_uri") == CONNECT_REDIRECT_URI)


def _count(env: Env, table: str) -> int:
    return int(env.scalar(f"SELECT count(*) FROM {table}"))  # noqa: S608


# ---------------------------------------------------------------- 티켓 발급


async def test_ticket_requires_session_and_csrf(env: Env) -> None:
    _, challenge = _pkce()
    body = {"intent": "connect", **_app_fields(challenge)}
    assert (await env.new_client().post(TICKET_PATH, json=body)).status_code == 401
    client = await env.login(ALICE)
    assert (await client.post(TICKET_PATH, json=body)).status_code == 403
    bad = await client.post(TICKET_PATH, json=body, headers={"X-CSRF-Token": "x"})
    assert bad.status_code == 403
    assert _count(env, "desktop_connect_ticket") == 0


async def test_ticket_binds_the_app_values_and_stores_only_a_hash(env: Env) -> None:
    client = await env.login(ALICE)
    _, challenge = _pkce()
    response = await _ticket(client, challenge)
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"url"}
    parts = urlsplit(response.json()["url"])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"https://orbit.test{BEGIN_PATH}"
    ticket = parse_qs(parts.query)["ticket"][0]
    assert len(ticket) >= 43
    dump = env.scalar("SELECT string_agg(t::text, ' ') FROM desktop_connect_ticket t")
    assert ticket not in dump
    assert env.scalar("SELECT code_challenge FROM desktop_connect_ticket") == challenge
    assert env.scalar("SELECT port FROM desktop_connect_ticket") == PORT
    assert env.scalar("SELECT app_state FROM desktop_connect_ticket") == APP_STATE


@pytest.mark.parametrize(
    "override",
    [
        {"code_challenge": "A" * 42},
        {"code_challenge": "A" * 42 + "+"},
        {"port": 80},
        {"port": 70000},
        {"port": "x"},
        {"app_state": "short"},
        {"app_state": "a" * 129},
        {"intent": "other"},
        {"intent": "connect", "connection_id": "6f9619ff-8b86-d011-b42d-00c04fc964ff"},
        {"intent": "docs_scope"},
        {"extra": 1},
    ],
)
async def test_ticket_rejects_malformed_or_forged_bodies(
    env: Env, override: dict[str, Any]
) -> None:
    client = await env.login(ALICE)
    _, challenge = _pkce()
    body = {"intent": "connect", **_app_fields(challenge), **override}
    response = await client.post(TICKET_PATH, json=body, headers=_csrf(client))
    assert response.status_code == 422
    assert _count(env, "desktop_connect_ticket") == 0


@pytest.mark.parametrize("missing", ["code_challenge", "port", "app_state"])
async def test_ticket_requires_the_app_values(env: Env, missing: str) -> None:
    client = await env.login(ALICE)
    _, challenge = _pkce()
    body = {"intent": "connect", **_app_fields(challenge)}
    del body[missing]
    assert (await client.post(TICKET_PATH, json=body, headers=_csrf(client))).status_code == 422


async def test_ticket_conflicts_match_the_web_start(env: Env) -> None:
    client, cid = await _connected(env)
    _, challenge = _pkce()
    again = await _ticket(client, challenge)
    assert (again.status_code, again.json()["detail"]) == (409, "already_connected")
    _grant_docs(env)
    result, _ = await _connect_via_app(env, client, "docs_scope", cid)
    assert result.json() == {"result": "docs_added"}
    done = await _ticket(client, challenge, "docs_scope", cid)
    assert (done.status_code, done.json()["detail"]) == (409, "docs_already_granted")


async def test_docs_ticket_for_another_workspaces_connection_is_not_found(env: Env) -> None:
    _, bob_connection = await _connected(env, BOB)
    alice = await env.login(ALICE)
    _, challenge = _pkce()
    response = await _ticket(alice, challenge, "docs_scope", bob_connection)
    assert (response.status_code, response.json()["detail"]) == (404, "not_found")
    assert _count(env, "desktop_connect_ticket") == 0


# ---------------------------------------------------------------- 안내 화면(GET)은 소비하지 않는다


async def test_get_begin_only_shows_the_continue_page_and_never_consumes_the_ticket(
    env: Env,
) -> None:
    client = await env.login(ALICE)
    _, challenge = _pkce()
    url = _begin_url(await _ticket(client, challenge))
    for _ in range(3):  # 링크 미리보기·프리페치가 여러 번 열어도
        page = await env.new_client().get(url)
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"
        assert 'method="post"' in page.text and "계속" in page.text
        assert "location" not in page.headers
        assert "orbit_connect" not in page.headers.get("set-cookie", "")
    assert env.scalar("SELECT used_at IS NULL FROM desktop_connect_ticket") is True
    assert _exchanges(env) == 0
    # 계속을 누르면 그제야 사용된다.
    browser = env.new_client()
    posted = await browser.post(BEGIN_PATH, data=_form(await browser.get(url)))
    assert posted.status_code == 303
    assert env.scalar("SELECT used_at IS NOT NULL FROM desktop_connect_ticket") is True


@pytest.mark.parametrize("query", ["", "?ticket=", "?ticket=short", "?ticket=" + "A" * 42 + "<"])
async def test_get_begin_rejects_malformed_tickets(env: Env, query: str) -> None:
    response = await env.new_client().get(BEGIN_PATH + query)
    assert response.status_code == 400
    assert "연결하지 못했습니다" in response.text
    assert "<form" not in response.text


# ---------------------------------------------------------------- POST begin 폼 CSRF 방어


async def _fresh_ticket(env: Env) -> tuple[httpx.AsyncClient, httpx.Response, str]:
    client = await env.login(ALICE)
    _, challenge = _pkce()
    return client, await _ticket(client, challenge), challenge


def _failed(response: httpx.Response) -> None:
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "location" not in response.headers
    assert "orbit_connect=" not in response.headers.get("set-cookie", "")
    assert "연결하지 못했습니다" in response.text


async def test_post_begin_requires_the_signed_value_and_the_matching_cookie(env: Env) -> None:
    _, ticket_response, _ = await _fresh_ticket(env)
    url = _begin_url(ticket_response)
    # 다른 사이트의 자동 제출 폼: 쿠키도 서명 값도 없다.
    ticket = parse_qs(urlsplit(url).query)["ticket"][0]
    _failed(await env.new_client().post(BEGIN_PATH, data={"ticket": ticket}))
    _failed(await env.new_client().post(BEGIN_PATH, data={"ticket": ticket, "token": "x.1.y"}))

    # 한 브라우저가 받은 서명 값을 다른 브라우저(쿠키 없음)에서 제출
    page_browser = env.new_client()
    form = _form(await page_browser.get(url))
    _failed(await env.new_client().post(BEGIN_PATH, data=form))

    # 쿠키 nonce가 다르다
    other_browser = env.new_client()
    await other_browser.get(url)  # 다른 nonce 쿠키
    _failed(await other_browser.post(BEGIN_PATH, data=form))

    # 서명 값이 변조됐다
    nonce, expires, mac = form["token"].split(".")
    forged = {"ticket": form["ticket"], "token": f"{nonce}.{int(expires) + 600}.{mac}"}
    _failed(await page_browser.post(BEGIN_PATH, data=forged))

    # 다른 티켓에 쓰려는 서명 값
    _, other_response, _ = await _fresh_ticket(env)
    other_ticket = parse_qs(urlsplit(_begin_url(other_response)).query)["ticket"][0]
    _failed(
        await page_browser.post(BEGIN_PATH, data={"ticket": other_ticket, "token": form["token"]})
    )
    assert env.scalar("SELECT count(*) FROM desktop_connect_ticket WHERE used_at IS NOT NULL") == 0


async def test_post_begin_form_value_expires(env: Env) -> None:
    _, ticket_response, _ = await _fresh_ticket(env)
    browser = env.new_client()
    form = _form(await browser.get(_begin_url(ticket_response)))
    env.clock.advance(timedelta(seconds=601))
    _failed(await browser.post(BEGIN_PATH, data=form))


async def test_ticket_expires_after_a_minute_and_is_single_use(env: Env) -> None:
    _, ticket_response, _ = await _fresh_ticket(env)
    url = _begin_url(ticket_response)
    browser = env.new_client()
    form = _form(await browser.get(url))
    assert (await browser.post(BEGIN_PATH, data=form)).status_code == 303
    # 같은 폼을 다시 제출해도(쿠키는 지워졌다) 실패하고, 새로 받은 폼으로도 티켓은 이미 소비됐다.
    _failed(await browser.post(BEGIN_PATH, data=form))
    again = env.new_client()
    _failed(await again.post(BEGIN_PATH, data=_form(await again.get(url))))

    _, late_response, _ = await _fresh_ticket(env)
    late = env.new_client()
    late_form = _form(await late.get(_begin_url(late_response)))
    env.clock.advance(timedelta(seconds=61))
    _failed(await late.post(BEGIN_PATH, data=late_form))


async def test_concurrent_posts_of_one_ticket_succeed_once(env: Env) -> None:
    _, ticket_response, _ = await _fresh_ticket(env)
    url = _begin_url(ticket_response)
    browsers = [env.new_client() for _ in range(4)]
    forms = [_form(await b.get(url)) for b in browsers]
    results = await asyncio.gather(
        *(b.post(BEGIN_PATH, data=f) for b, f in zip(browsers, forms, strict=True))
    )
    assert sorted(r.status_code for r in results) == [303, 400, 400, 400]


async def test_begin_sends_the_same_authorization_request_as_the_web_start(env: Env) -> None:
    client, ticket_response, _ = await _fresh_ticket(env)
    posted = await _consent(env, env.new_client(), ticket_response)
    params = parse_qs(urlsplit(posted.headers["location"]).query)
    assert set(params["scope"][0].split()) == set(REQUEST_SCOPES)
    assert params["access_type"] == ["offline"] and params["prompt"] == ["consent"]
    assert params["include_granted_scopes"] == ["true"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [CONNECT_REDIRECT_URI]
    cookie = next(
        c for c in posted.headers.get_list("set-cookie") if c.startswith("orbit_connect=")
    ).lower()
    for attribute in ("httponly", "samesite=lax", "secure", "max-age=600"):
        assert attribute in cookie
    assert "path=/api/connections/google" in cookie
    assert posted.headers["cache-control"] == "no-store"
    assert posted.headers["referrer-policy"] == "no-referrer"
    # 앱의 challenge·포트는 Google로 가는 주소에 없다(서명 쿠키에만 있다).
    assert str(PORT) not in posted.headers["location"]
    assert APP_STATE not in posted.headers["location"]
    assert client  # 앱 세션은 그대로다


async def test_extra_form_fields_cannot_change_the_intent(env: Env) -> None:
    client, cid = await _connected(env)
    await client.delete(f"/api/connections/{cid}", headers=_csrf(client))
    _, challenge = _pkce()
    url = _begin_url(await _ticket(client, challenge))
    browser = env.new_client()
    form = _form(await browser.get(url))
    posted = await browser.post(
        BEGIN_PATH, data={**form, "intent": "docs_scope", "connection_id": cid}
    )
    params = parse_qs(urlsplit(posted.headers["location"]).query)
    assert set(params["scope"][0].split()) == set(REQUEST_SCOPES)
    assert "login_hint" not in params


async def test_post_begin_rechecks_the_connection_state(env: Env) -> None:
    client, cid = await _connected(env)
    _grant_docs(env)
    _, challenge = _pkce()
    docs = await _ticket(client, challenge, "docs_scope", cid)
    browser = env.new_client()
    form = _form(await browser.get(_begin_url(docs)))
    await client.delete(f"/api/connections/{cid}", headers=_csrf(client))
    _failed(await browser.post(BEGIN_PATH, data=form))

    fresh = await _ticket(client, challenge)
    browser2 = env.new_client()
    form2 = _form(await browser2.get(_begin_url(fresh)))
    await _connect(env, client)  # 그 사이에 웹으로 연결했다
    _failed(await browser2.post(BEGIN_PATH, data=form2))


# ------------------------------------------------------- 연결 CSRF 공격 시나리오(가장 중요)


async def test_attackers_ticket_opened_by_the_victim_saves_nothing(env: Env) -> None:
    attacker = await env.login(BOB)
    attacker_verifier, attacker_challenge = _pkce()
    ticket = await _ticket(attacker, attacker_challenge)  # 공격자의 티켓 URL
    env.set_identity(ALICE)  # 피해자의 Google 계정으로 동의한다
    victim_browser = env.new_client()  # 세션 없는 피해자의 시스템 브라우저
    await _consent(env, victim_browser, ticket)
    callback = await _google_callback(victim_browser, env)

    # 완료 코드는 피해자 PC의 127.0.0.1로만 간다. 브라우저에서 저장되는 것은 없다.
    query = _loopback(callback)
    assert _exchanges(env) == 0  # 토큰 교환 자체를 하지 않았다
    assert _rows(env) == []  # 어느 워크스페이스에도 연결이 저장되지 않았다
    assert (await attacker.get("/api/connections")).json()["connections"] == []
    assert _count(env, "desktop_connect_pending") == 1
    # 완료 코드는 Google 인가 코드가 아니고 응답 어디에도 인가 코드·토큰이 없다.
    assert CODE not in callback.headers["location"]

    # 공격자는 코드를 받지 못했다. 코드 없이·추측한 코드로 자기 세션에서 완료를 시도해도 실패한다.
    guesses = [secrets.token_urlsafe(32) for _ in range(3)] + ["A" * 43, CODE.ljust(43, "x")]
    for guess in guesses:
        failed = await _complete(attacker, guess, attacker_verifier)
        assert (failed.status_code, failed.json()) == (400, INVALID_GRANT)
    no_code = await attacker.post(
        COMPLETE_PATH, json={"code_verifier": attacker_verifier}, headers=_csrf(attacker)
    )
    assert no_code.status_code == 422
    assert _rows(env) == [] and _exchanges(env) == 0

    # 피해자의 앱 세션이 있어도(다른 워크스페이스) 그 코드는 제출되지 않는다.
    victim_app = await env.login(ALICE)
    verifier, _ = _pkce()
    stolen = await _complete(victim_app, query["code"][0], verifier)
    assert (stolen.status_code, stolen.json()) == (400, INVALID_GRANT)
    assert _rows(env) == []


# ---------------------------------------------------------------- 정상 연결과 완료


async def test_connect_callback_stores_a_sealed_pending_row_and_no_connection(env: Env) -> None:
    client = await env.login(ALICE)
    verifier, challenge = _pkce()
    browser, code = await _handoff(env, await _ticket(client, challenge))
    assert _rows(env) == []
    assert _exchanges(env) == 0
    assert _count(env, "desktop_connect_pending") == 1
    assert env.scalar("SELECT used_at IS NULL FROM desktop_connect_pending") is True
    assert env.scalar("SELECT code_challenge FROM desktop_connect_pending") == challenge
    assert env.scalar("SELECT intent FROM desktop_connect_pending") == "connect"
    # 인가 코드·검증기·완료 코드는 평문으로 저장하지 않는다.
    dump = env.scalar("SELECT string_agg(t::text, ' ') FROM desktop_connect_pending t")
    blob = env.scalar("SELECT ciphertext || nonce || wrapped_dek FROM desktop_connect_pending")
    for secret in (CODE, code, verifier):
        assert secret not in dump
        assert secret.encode() not in bytes(blob)
    assert env.scalar("SELECT code_hash FROM desktop_connect_pending") != code
    cookie = browser.cookies.get(
        "orbit_connect", domain="orbit.test", path="/api/connections/google"
    )
    assert cookie is None  # 쓰고 나서 지운다


async def test_the_app_completes_the_connection_with_its_session(env: Env) -> None:
    client = await env.login(ALICE)
    response, _ = await _connect_via_app(env, client)
    assert (response.status_code, response.json()) == (200, {"result": "connected"})
    assert response.headers["cache-control"] == "no-store"
    assert _exchanges(env) == 1
    [item] = (await client.get("/api/connections")).json()["connections"]
    assert item["status"] == "active" and item["account_email"] == ALICE["email"]
    assert env.scalar("SELECT used_at IS NOT NULL FROM desktop_connect_pending") is True
    body = (await client.get("/api/connections")).text
    assert CODE not in body


async def test_docs_scope_flow_adds_the_drive_scope_to_the_same_connection(env: Env) -> None:
    client, cid = await _connected(env)
    [before] = _rows(env)
    _grant_docs(env)
    verifier, challenge = _pkce()
    ticket = await _ticket(client, challenge, "docs_scope", cid)
    browser = env.new_client()
    posted = await _consent(env, browser, ticket)
    params = parse_qs(urlsplit(posted.headers["location"]).query)
    assert set(params["scope"][0].split()) == {"openid", "email", DRIVE}
    assert params["login_hint"] == [ALICE["email"]]
    query = _loopback(await _google_callback(browser, env))
    assert (await client.get("/api/connections")).json()["connections"][0]["docs_granted"] is False
    response = await _complete(client, query["code"][0], verifier)
    assert (response.status_code, response.json()) == (200, {"result": "docs_added"})
    [after] = _rows(env)
    assert after.id == before.id and DRIVE in after.scopes
    assert (await client.get("/api/connections")).json()["connections"][0]["docs_granted"] is True


async def test_completion_applies_the_web_save_rules(env: Env) -> None:
    client, cid = await _connected(env)
    [before] = _rows(env)
    _grant_docs(env)
    verifier, challenge = _pkce()
    ticket = await _ticket(client, challenge, "docs_scope", cid)
    browser = env.new_client()
    await _consent(env, browser, ticket)
    env.set_identity(BOB)  # 다른 Google 계정으로 동의했다
    query = _loopback(await _google_callback(browser, env))
    response = await _complete(client, query["code"][0], verifier)
    assert (response.status_code, response.json()) == (
        200,
        {"result": "error", "error": "account_mismatch"},
    )
    [after] = _rows(env)
    assert (after.scopes, after.token_ciphertext) == (before.scopes, before.token_ciphertext)


async def test_missing_scope_is_reported_as_an_error_result(env: Env) -> None:
    client = await env.login(ALICE)
    env.mock.granted_scope = "openid email"  # type: ignore[attr-defined]
    response, _ = await _connect_via_app(env, client)
    assert response.status_code == 200
    assert response.json() == {"result": "error", "error": "scope_missing"}
    assert (await client.get("/api/connections")).json()["connections"][0]["status"] == "error"


# ---------------------------------------------------------------- 완료 실패는 구분 없는 400


async def _pending(env: Env) -> tuple[httpx.AsyncClient, str, str]:
    client = await env.login(ALICE)
    verifier, challenge = _pkce()
    _, code = await _handoff(env, await _ticket(client, challenge))
    return client, code, verifier


async def test_wrong_verifier_is_rejected_without_burning_the_code(env: Env) -> None:
    client, code, verifier = await _pending(env)
    wrong, _ = _pkce()
    bad = await _complete(client, code, wrong)
    assert (bad.status_code, bad.json()) == (400, INVALID_GRANT)
    assert _rows(env) == [] and _exchanges(env) == 0
    assert (await _complete(client, code, verifier)).json() == {"result": "connected"}


async def test_expired_completion_code_is_rejected(env: Env) -> None:
    client, code, verifier = await _pending(env)
    env.clock.advance(timedelta(seconds=301))
    response = await _complete(client, code, verifier)
    assert (response.status_code, response.json()) == (400, INVALID_GRANT)
    assert _rows(env) == [] and _exchanges(env) == 0


async def test_completion_code_is_valid_until_the_last_second(env: Env) -> None:
    client, code, verifier = await _pending(env)
    env.clock.advance(timedelta(seconds=299))
    assert (await _complete(client, code, verifier)).status_code == 200


async def test_reused_completion_code_fails(env: Env) -> None:
    client, code, verifier = await _pending(env)
    assert (await _complete(client, code, verifier)).json() == {"result": "connected"}
    again = await _complete(client, code, verifier)
    assert (again.status_code, again.json()) == (400, INVALID_GRANT)
    assert _exchanges(env) == 1  # 두 번째는 Google을 부르지 않는다


async def test_another_session_cannot_complete_it(env: Env) -> None:
    _, code, verifier = await _pending(env)
    saved = (
        env.mock.challenge,
        env.mock.nonce,
        env.mock.state,
    )  # 로그인이 모의 Google 값을 덮어쓴다
    bob = await env.login(BOB)  # 다른 워크스페이스·사용자
    response = await _complete(bob, code, verifier)
    assert (response.status_code, response.json()) == (400, INVALID_GRANT)
    assert _rows(env) == []
    assert env.scalar("SELECT used_at IS NULL FROM desktop_connect_pending") is True
    alice = await env.login(ALICE)  # 같은 사용자의 새 세션은 완료할 수 있다
    env.mock.challenge, env.mock.nonce, env.mock.state = saved
    env.set_identity(ALICE)
    assert (await _complete(alice, code, verifier)).json() == {"result": "connected"}


async def test_all_completion_failures_look_the_same(env: Env) -> None:
    client, code, verifier = await _pending(env)
    other, _ = _pkce()
    responses = [
        await _complete(client, "unknown-" + "x" * 40, verifier),
        await _complete(client, code, other),
        await _complete(client, "짧", verifier),
        await _complete(client, code, "short"),
    ]
    for response in responses:
        assert (response.status_code, response.json()) == (400, INVALID_GRANT)
    assert _rows(env) == [] and _exchanges(env) == 0


async def test_completion_requires_session_and_csrf(env: Env) -> None:
    client, code, verifier = await _pending(env)
    body = {"code": code, "code_verifier": verifier}
    assert (await env.new_client().post(COMPLETE_PATH, json=body)).status_code == 401
    assert (await client.post(COMPLETE_PATH, json=body)).status_code == 403
    bad = await client.post(COMPLETE_PATH, json=body, headers={"X-CSRF-Token": "x"})
    assert bad.status_code == 403
    assert (
        await client.post(COMPLETE_PATH, json={"code": code}, headers=_csrf(client))
    ).status_code == 422
    assert env.scalar("SELECT used_at IS NULL FROM desktop_connect_pending") is True


async def test_concurrent_completions_connect_once(env: Env) -> None:
    client, code, verifier = await _pending(env)
    results = await asyncio.gather(*(_complete(client, code, verifier) for _ in range(4)))
    assert sorted(r.status_code for r in results) == [200, 400, 400, 400]
    assert _exchanges(env) == 1


# ---------------------------------------------------------------- 콜백 실패와 웹 흐름 회귀


async def test_desktop_callback_failures_go_to_the_apps_loopback(env: Env) -> None:
    client = await env.login(ALICE)
    _, challenge = _pkce()
    for params, kind in (({"state": "forged"}, "state"), ({"code": ""}, "exchange")):
        browser = env.new_client()
        await _consent(env, browser, await _ticket(client, challenge))
        query = _loopback(await _google_callback(browser, env, **params))
        assert query == {"error": [kind], "state": [APP_STATE]}
    browser = env.new_client()
    await _consent(env, browser, await _ticket(client, challenge))
    denied = await browser.get(
        "/api/connections/google/callback",
        params={"error": "access_denied", "state": env.mock.state},
    )
    assert _loopback(denied) == {"error": ["denied"], "state": [APP_STATE]}
    assert _count(env, "desktop_connect_pending") == 0
    assert _rows(env) == [] and _exchanges(env) == 0


async def test_tampered_desktop_cookie_is_not_trusted(env: Env) -> None:
    client = await env.login(ALICE)
    _, challenge = _pkce()
    browser = env.new_client()
    await _consent(env, browser, await _ticket(client, challenge))
    cookie = browser.cookies.get(
        "orbit_connect", domain="orbit.test", path="/api/connections/google"
    )
    assert cookie
    payload, signature = cookie.split(".")
    response = await env.new_client().get(
        "/api/connections/google/callback",
        params={"code": CODE, "state": env.mock.state},
        headers={"Cookie": f"orbit_connect={payload[:-2]}AA.{signature}"},
    )
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["location"]).query)["connection_error"] == [
        "unauthenticated"
    ]
    assert _count(env, "desktop_connect_pending") == 0 and _rows(env) == []


async def test_web_start_cookie_without_a_session_is_still_rejected(env: Env) -> None:
    client = await env.login(ALICE)
    start = await client.post("/api/connections/google/start", headers=_csrf(client))
    env.mock.observe_authorization(start.json()["authorization_url"])
    cookie = client.cookies.get(
        "orbit_connect", domain="orbit.test", path="/api/connections/google"
    )
    assert cookie
    response = await env.new_client().get(
        "/api/connections/google/callback",
        params={"code": CODE, "state": env.mock.state},
        headers={"Cookie": f"orbit_connect={cookie}"},
    )
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["location"]).query)["connection_error"] == [
        "unauthenticated"
    ]
    assert _rows(env) == []


async def test_web_connect_is_unchanged(env: Env) -> None:
    client = await env.login(ALICE)
    response = await _connect(env, client)
    assert (response.status_code, response.headers["location"]) == (302, "/?connection=connected")
    assert (
        _count(env, "desktop_connect_ticket") == 0 and _count(env, "desktop_connect_pending") == 0
    )


# ---------------------------------------------------------------- 정리와 권한


async def test_expired_rows_are_purged_after_an_hour(env: Env) -> None:
    client = await env.login(ALICE)
    _, challenge = _pkce()
    await _ticket(client, challenge)
    await _handoff(env, await _ticket(client, challenge))
    assert await purge_expired_handoffs(env.db, env.clock) == 0
    env.clock.advance(timedelta(seconds=300 + 3600 + 1))
    assert await purge_expired_handoffs(env.db, env.clock) == 3  # 티켓 2개 + 대기 행 1개
    assert _count(env, "desktop_connect_ticket") == 0
    assert _count(env, "desktop_connect_pending") == 0


async def test_tables_are_isolated_and_write_restricted_for_the_app_role(env: Env) -> None:
    alice = await env.login(ALICE)
    _, challenge = _pkce()
    await _handoff(env, await _ticket(alice, challenge))
    ws = env.scalar("SELECT workspace_id FROM desktop_connect_ticket")
    for sql in (
        "SELECT count(*) FROM desktop_connect_ticket",
        "UPDATE desktop_connect_ticket SET used_at = now()",
        "DELETE FROM desktop_connect_ticket",
        "UPDATE desktop_connect_pending SET expires_at = now()",
        "DELETE FROM desktop_connect_pending",
    ):
        with pytest.raises(DBAPIError) as info:
            async with env.db.workspace_session(ws) as s:
                await s.execute(text(sql))
        assert getattr(info.value.orig, "sqlstate", None) == "42501", sql
    # 대기 행은 자기 워크스페이스에서만 보인다.
    bob = await env.login(BOB)
    other_ws = (await bob.get("/api/session")).json()["workspace_id"]
    async with env.db.workspace_session(ws) as s:
        mine = (await s.execute(text("SELECT count(*) FROM desktop_connect_pending"))).scalar_one()
    async with env.db.workspace_session(other_ws) as s:
        theirs = (
            await s.execute(text("SELECT count(*) FROM desktop_connect_pending"))
        ).scalar_one()
    assert (mine, theirs) == (1, 0)
    # 다른 워크스페이스 ID로는 삽입할 수 없다(RLS WITH CHECK).
    user = env.scalar("SELECT user_id FROM desktop_connect_ticket")
    with pytest.raises(DBAPIError) as info:
        async with env.db.workspace_session(ws) as s:
            await s.execute(
                text(
                    "INSERT INTO desktop_connect_ticket (id, workspace_id, user_id, ticket_hash,"
                    " intent, code_challenge, port, app_state, created_at, expires_at)"
                    " VALUES (gen_random_uuid(), :other, :u, :h, 'connect', :c, 5000, :a,"
                    " now(), now())"
                ),
                {"other": other_ws, "u": user, "h": "0" * 64, "c": "A" * 43, "a": "a" * 20},
            )
    assert getattr(info.value.orig, "sqlstate", None) == "42501"
