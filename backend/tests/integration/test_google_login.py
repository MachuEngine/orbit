"""Google 로그인 API와 사용자·워크스페이스 생성 (ADR-0016 §1). 모의 Google, 합성 사용자만 쓴다."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from google_mock import (
    CLIENT_ID,
    CLIENT_SECRET,
    CODE,
    REDIRECT_URI,
    SIGNING_KEY,
    MockGoogle,
    attacker_key,
)
from integration.support import WorkspaceFactory
from orbit.api.app import create_app
from orbit.auth import issue_session
from orbit.auth.sessions import hash_token
from orbit.auth.users import login_upsert_user
from orbit.core.clock import FixedClock
from orbit.core.config import Settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase, SyncDatabase

NOW = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
BASE = "https://orbit.test"
ALICE = {"sub": "test-sub-alice", "email": "alice@example.test", "name": "앨리스 테스트"}
BOB = {"sub": "test-sub-bob", "email": "bob@example.test", "name": "밥 테스트"}
SUB_PATTERN = "test-sub-%"


@dataclass
class Env:
    clock: FixedClock
    ids: Uuid7Generator
    mock: MockGoogle
    transport: httpx.ASGITransport
    db: AsyncDatabase
    owner: Engine
    clients: list[httpx.AsyncClient]

    def new_client(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=self.transport, base_url=BASE)
        self.clients.append(client)
        return client

    def set_identity(self, who: dict[str, str]) -> None:
        self.mock.edit(sub=who["sub"], email=who["email"], name=who["name"])

    async def start(self, client: httpx.AsyncClient) -> httpx.Response:
        response = await client.get("/api/auth/google/start")
        assert response.status_code == 302, response.text
        self.mock.observe_authorization(response.headers["location"])
        return response

    async def callback(
        self, client: httpx.AsyncClient, *, state: str | None = None, **extra: str
    ) -> httpx.Response:
        params = {"code": CODE, "state": state if state is not None else self.mock.state}
        params.update(extra)
        return await client.get("/api/auth/google/callback", params=params)

    async def login(self, who: dict[str, str]) -> httpx.AsyncClient:
        """모의 Google로 정상 로그인한 클라이언트(쿠키를 가진다)."""
        self.set_identity(who)
        client = self.new_client()
        await self.start(client)
        response = await self.callback(client)
        assert response.status_code == 302
        assert response.headers["location"] == "/", response.headers["location"]
        return client

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.owner.connect() as conn:
            value = conn.execute(text(sql), params).scalar_one()
            conn.rollback()
        return value


@pytest.fixture
def owner_engine(owner_url: str) -> Iterator[Engine]:
    engine = create_engine(owner_url, pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


def _cleanup(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM procrastinate_jobs WHERE args->>'workspace_id' IN"
                " (SELECT w.id::text FROM workspace w JOIN app_user u ON u.id = w.owner_user_id"
                " WHERE u.google_sub LIKE :p)"
            ),
            {"p": SUB_PATTERN},
        )
        conn.execute(
            text(
                "DELETE FROM workspace WHERE owner_user_id IN"
                " (SELECT id FROM app_user WHERE google_sub LIKE :p)"
            ),
            {"p": SUB_PATTERN},
        )
        conn.execute(text("DELETE FROM app_user WHERE google_sub LIKE :p"), {"p": SUB_PATTERN})


@pytest.fixture
async def env(app_url: str, owner_engine: Engine, tmp_path: Path) -> AsyncIterator[Env]:
    _cleanup(owner_engine)
    key_file = tmp_path / "signing.key"
    key_file.write_text(SIGNING_KEY, encoding="utf-8")
    clock = FixedClock(NOW)
    ids = Uuid7Generator(clock)
    mock = MockGoogle(clock)
    settings = Settings(
        database_url=SecretStr(app_url),
        env="prod",
        google_client_id=CLIENT_ID,
        google_client_secret=SecretStr(CLIENT_SECRET),
        google_redirect_uri=REDIRECT_URI,
        session_signing_key_file=str(key_file),
        login_allowed_emails="Alice@example.test, bob@example.test",
        google_auth_url="https://mock-google.test/o/oauth2/v2/auth",
        google_token_url="https://mock-google.test/token",
        google_jwks_url="https://mock-google.test/certs",
    )
    app = create_app(settings, clock=clock, ids=ids, google_transport=mock.transport())
    async with app.router.lifespan_context(app):
        db: AsyncDatabase = app.state.db
        made = Env(
            clock=clock,
            ids=ids,
            mock=mock,
            transport=httpx.ASGITransport(app=app),
            db=db,
            owner=owner_engine,
            clients=[],
        )
        try:
            yield made
        finally:
            for client in made.clients:
                await client.aclose()
            _cleanup(owner_engine)


def _error_of(response: httpx.Response) -> str:
    assert response.status_code == 302
    location = response.headers["location"]
    parts = urlsplit(location)
    assert parts.path == "/"
    return parse_qs(parts.query)["login_error"][0]


def _cookie_lines(response: httpx.Response) -> dict[str, str]:
    return {c.split("=", 1)[0]: c.lower() for c in response.headers.get_list("set-cookie")}


# ---------------------------------------------------------------- 시작


async def test_start_redirects_to_google_with_signed_flow_cookie(env: Env) -> None:
    client = env.new_client()
    response = await env.start(client)
    params = env.mock.observe_authorization(response.headers["location"])
    assert params["scope"] == "openid email profile"
    assert params["code_challenge_method"] == "S256"
    cookie = _cookie_lines(response)["orbit_login"]
    for attribute in ("httponly", "samesite=lax", "secure", "max-age=600", "path=/api/auth/google"):
        assert attribute in cookie, attribute


# ---------------------------------------------------------------- 정상 로그인


async def test_login_creates_user_workspace_membership_and_session(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    response = await env.callback(client)
    assert (response.status_code, response.headers["location"]) == (302, "/")
    cookies = _cookie_lines(response)
    assert "httponly" in cookies["orbit_session"]
    assert "httponly" not in cookies["orbit_csrf"]
    for name in ("orbit_session", "orbit_csrf"):
        assert "secure" in cookies[name]  # dev가 아니면 Secure
        assert "samesite=lax" in cookies[name]
    assert "max-age=0" in cookies["orbit_login"]  # 임시 쿠키는 쓰고 나서 지운다
    # 토큰 교환은 PKCE 검증기를 보냈고 모의 서버가 S256 대조에 성공했다.
    assert len(env.mock.token_requests) == 1

    info = (await client.get("/api/session")).json()
    assert info["authenticated"] is True
    assert info["display_name"] == "앨리스 테스트"
    assert info["email"] == "alice@example.test"
    assert info["google_login"] is True
    assert info["dev_login"] is False

    ws = info["workspace_id"]
    assert env.scalar("SELECT count(*) FROM app_user WHERE google_sub = 'test-sub-alice'") == 1
    assert env.scalar("SELECT type FROM workspace WHERE id = :ws", ws=ws) == "personal"
    assert str(env.scalar("SELECT owner_user_id FROM workspace WHERE id = :ws", ws=ws)) == str(
        env.scalar("SELECT id FROM app_user WHERE google_sub = 'test-sub-alice'")
    )
    assert env.scalar("SELECT role FROM membership WHERE workspace_id = :ws", ws=ws) == "owner"
    assert (
        env.scalar(
            "SELECT count(*) FROM auth_session WHERE workspace_id = :ws AND user_id IS NOT NULL",
            ws=ws,
        )
        == 1
    )


async def test_session_cookie_from_google_login_authorizes_the_api(env: Env) -> None:
    client = await env.login(ALICE)
    csrf = client.cookies.get("orbit_csrf")
    assert csrf
    created = await client.post(
        "/api/runs", json={"message": "안녕"}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    assert (await client.get(f"/api/runs/{run_id}")).status_code == 200


async def test_second_login_reuses_user_and_workspace_with_fresh_session(env: Env) -> None:
    first = await env.login(ALICE)
    info1 = (await first.get("/api/session")).json()
    token1 = first.cookies.get("orbit_session")
    login_at = env.scalar("SELECT last_login_at FROM app_user WHERE google_sub = 'test-sub-alice'")

    env.clock.advance(timedelta(hours=1))
    second = await env.login({**ALICE, "name": "앨리스 개명"})
    info2 = (await second.get("/api/session")).json()
    token2 = second.cookies.get("orbit_session")

    assert info2["workspace_id"] == info1["workspace_id"]
    assert token1 and token2 and token1 != token2  # 로그인마다 새 토큰(세션 고정 방지)
    assert env.scalar("SELECT count(*) FROM app_user WHERE google_sub LIKE 'test-sub-%'") == 1
    assert (
        env.scalar(
            "SELECT count(*) FROM workspace WHERE owner_user_id ="
            " (SELECT id FROM app_user WHERE google_sub = 'test-sub-alice')"
        )
        == 1
    )
    assert info2["display_name"] == "앨리스 개명"
    assert env.scalar(
        "SELECT last_login_at FROM app_user WHERE google_sub = 'test-sub-alice'"
    ) == login_at + timedelta(hours=1)
    # 앞선 세션도 만료 전까지는 유효하다(다른 기기 로그인을 끊지 않는다).
    assert (await first.get("/api/session")).json()["authenticated"] is True


async def test_allow_list_is_case_insensitive_and_identity_is_the_sub_not_the_email(
    env: Env,
) -> None:
    env.set_identity(ALICE)
    env.mock.edit(email="ALICE@Example.TEST")
    client = env.new_client()
    await env.start(client)
    assert (await env.callback(client)).headers["location"] == "/"
    # 같은 sub로 이메일이 바뀌어도(허용 목록 안) 같은 사용자다.
    env.mock.edit(email="bob@example.test")
    client2 = env.new_client()
    await env.start(client2)
    assert (await env.callback(client2)).headers["location"] == "/"
    assert env.scalar("SELECT count(*) FROM app_user WHERE google_sub = 'test-sub-alice'") == 1
    assert env.scalar("SELECT email FROM app_user WHERE google_sub = 'test-sub-alice'") == (
        "bob@example.test"
    )


# ---------------------------------------------------------------- 실패: 세션·사용자를 만들지 않는다


async def _assert_rejected(
    env: Env, client: httpx.AsyncClient, response: httpx.Response, kind: str
) -> None:
    assert _error_of(response) == kind
    cookies = _cookie_lines(response)
    assert "orbit_session" not in cookies
    assert "max-age=0" in cookies["orbit_login"]
    assert (await client.get("/api/session")).json()["authenticated"] is False
    assert env.scalar("SELECT count(*) FROM app_user WHERE google_sub LIKE 'test-sub-%'") == 0
    assert env.scalar("SELECT count(*) FROM auth_session WHERE user_id IS NOT NULL") == 0


async def test_state_mismatch_is_rejected(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    response = await env.callback(client, state="forged-state")
    await _assert_rejected(env, client, response, "state")
    assert env.mock.token_requests == []


async def test_missing_state_or_code_is_rejected(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    response = await client.get("/api/auth/google/callback", params={"code": CODE})
    await _assert_rejected(env, client, response, "state")
    await env.start(client)
    response = await client.get("/api/auth/google/callback", params={"state": env.mock.state})
    await _assert_rejected(env, client, response, "exchange")


async def test_nonce_mismatch_is_rejected(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    env.mock.edit(nonce="another-nonce")
    await _assert_rejected(env, client, await env.callback(client), "nonce")


async def test_expired_id_token_is_rejected(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    env.mock.edit(exp=int(NOW.timestamp()) - 1)
    await _assert_rejected(env, client, await env.callback(client), "id_token")


async def test_audience_mismatch_is_rejected(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    env.mock.edit(aud="someone-elses-client-id")
    await _assert_rejected(env, client, await env.callback(client), "id_token")


async def test_forged_signature_is_rejected(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    env.mock.sign_key = attacker_key()
    await _assert_rejected(env, client, await env.callback(client), "id_token")


async def test_unverified_email_is_rejected(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    env.mock.edit(email_verified=False)
    await _assert_rejected(env, client, await env.callback(client), "email_unverified")


async def test_email_outside_allow_list_is_rejected_without_creating_a_user(env: Env) -> None:
    env.set_identity({"sub": "test-sub-carol", "email": "carol@example.test", "name": "캐럴"})
    client = env.new_client()
    await env.start(client)
    await _assert_rejected(env, client, await env.callback(client), "not_allowed")


async def test_google_outage_and_token_failure(env: Env) -> None:
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    env.mock.network_down = True
    await _assert_rejected(env, client, await env.callback(client), "unavailable")
    env.mock.network_down = False
    await env.start(client)
    env.mock.token_status = 400
    await _assert_rejected(env, client, await env.callback(client), "exchange")


async def test_user_denied_consent(env: Env) -> None:
    client = env.new_client()
    await env.start(client)
    response = await client.get(
        "/api/auth/google/callback", params={"error": "access_denied", "state": env.mock.state}
    )
    await _assert_rejected(env, client, response, "denied")
    assert env.mock.token_requests == []


async def test_flow_cookie_missing_tampered_or_expired(env: Env) -> None:
    env.set_identity(ALICE)
    anonymous = env.new_client()
    await env.start(anonymous)
    state = env.mock.state

    # 쿠키 없이 콜백
    bare = env.new_client()
    await _assert_rejected(env, bare, await env.callback(bare, state=state), "expired")

    # 서명 쿠키를 변조해 공격자의 state를 심는다.
    victim = env.new_client()
    start = await env.start(victim)
    cookie = victim.cookies.get("orbit_login", domain="orbit.test", path="/api/auth/google")
    assert cookie
    payload, signature = cookie.split(".")
    tampered = f"{payload[:-2]}AA.{signature}"
    forger = env.new_client()
    response = await forger.get(
        "/api/auth/google/callback",
        params={"code": CODE, "state": env.mock.state},
        headers={"Cookie": f"orbit_login={tampered}"},
    )
    await _assert_rejected(env, forger, response, "expired")
    assert start.status_code == 302

    # 10분이 지난 쿠키
    late = env.new_client()
    await env.start(late)
    env.clock.advance(timedelta(minutes=10))
    await _assert_rejected(env, late, await env.callback(late), "expired")
    assert env.mock.token_requests == []


# ---------------------------------------------------------------- 사용자 간 격리


async def test_other_users_session_cannot_read_a_workspace_it_does_not_own(env: Env) -> None:
    alice = await env.login(ALICE)
    bob = await env.login(BOB)
    alice_info = (await alice.get("/api/session")).json()
    bob_info = (await bob.get("/api/session")).json()
    assert alice_info["workspace_id"] != bob_info["workspace_id"]
    assert bob_info["email"] == "bob@example.test"
    assert alice_info["email"] == "alice@example.test"  # 본인 정보만

    csrf = alice.cookies.get("orbit_csrf")
    assert csrf
    created = await alice.post(
        "/api/runs", json={"message": "비공개"}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    assert (await alice.get(f"/api/runs/{run_id}")).status_code == 200
    assert (await bob.get(f"/api/runs/{run_id}")).status_code == 404
    assert (await bob.get(f"/api/runs/{run_id}/events")).status_code == 404


async def test_app_role_cannot_read_users_and_sessions_bind_only_to_members(
    env: Env, raw_engine: Engine, sync_db: SyncDatabase
) -> None:
    alice = await env.login(ALICE)
    bob = await env.login(BOB)
    ws_alice = uuid.UUID((await alice.get("/api/session")).json()["workspace_id"])
    ws_bob = uuid.UUID((await bob.get("/api/session")).json()["workspace_id"])
    alice_user = env.scalar("SELECT id FROM app_user WHERE google_sub = 'test-sub-alice'")

    # orbit_app은 app_user 테이블을 읽지 못한다.
    with raw_engine.connect() as conn, pytest.raises(DBAPIError):
        conn.execute(text("SELECT count(*) FROM app_user"))
    # membership은 현재 워크스페이스의 행만 보인다.
    with sync_db.workspace_session(ws_bob) as s:
        rows = s.execute(text("SELECT workspace_id FROM membership")).all()
    assert [r[0] for r in rows] == [ws_bob]
    with raw_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM membership")).scalar_one() == 0
        conn.rollback()
    # orbit_app은 membership·app_user에 쓰지 못한다.
    with sync_db.workspace_session(ws_bob) as s, pytest.raises(DBAPIError):
        s.execute(
            text("INSERT INTO membership (workspace_id, user_id, role) VALUES (:w, :u, 'owner')"),
            {"w": ws_bob, "u": alice_user},
        )
    # 멤버가 아닌 사용자를 세션에 묶을 수 없다(복합 FK).
    with sync_db.workspace_session(ws_bob) as s, pytest.raises(IntegrityError):
        s.execute(
            text(
                "INSERT INTO auth_session (id, workspace_id, user_id, token_hash,"
                " csrf_token_hash, created_at, expires_at)"
                " VALUES (:id, :w, :u, :h, :h, :now, :now)"
            ),
            {"id": uuid.uuid4(), "w": ws_bob, "u": alice_user, "h": "a" * 64, "now": NOW},
        )
    assert ws_alice != ws_bob


async def test_profile_function_returns_only_the_token_owners_row(env: Env) -> None:
    alice = await env.login(ALICE)
    bob = await env.login(BOB)
    token_a = alice.cookies.get("orbit_session")
    token_b = bob.cookies.get("orbit_session")
    assert token_a and token_b
    row_a = await env.db.unscoped_auth_session_profile(hash_token(token_a))
    row_b = await env.db.unscoped_auth_session_profile(hash_token(token_b))
    assert row_a is not None and row_b is not None
    assert (row_a[1], row_b[1]) == ("alice@example.test", "bob@example.test")
    for probe in (token_a, "", "%", hash_token("none"), str(row_a[0]), hash_token(token_a).upper()):
        assert await env.db.unscoped_auth_session_profile(probe) is None, probe


async def test_session_without_user_has_no_profile(env: Env, workspaces: WorkspaceFactory) -> None:
    ws = workspaces.create()
    issued = await issue_session(
        env.db, workspace_id=ws, clock=env.clock, ids=env.ids, ttl_seconds=60
    )
    assert await env.db.unscoped_auth_session_profile(hash_token(issued.token)) is None
    client = env.new_client()
    client.cookies.set("orbit_session", issued.token, domain="orbit.test")
    info = (await client.get("/api/session")).json()
    assert info["authenticated"] is True
    assert (info["display_name"], info["email"]) == (None, None)


async def test_concurrent_first_logins_create_one_user_and_workspace(env: Env) -> None:
    async def attempt() -> tuple[uuid.UUID, uuid.UUID, bool]:
        result = await login_upsert_user(
            env.db,
            google_sub="test-sub-race",
            email="race@example.test",
            display_name="경합",
            clock=env.clock,
            ids=env.ids,
        )
        return result.user_id, result.workspace_id, result.created

    results = await asyncio.gather(*(attempt() for _ in range(6)))
    assert len({(user, ws) for user, ws, _ in results}) == 1
    assert sum(1 for *_, created in results if created) == 1
    assert env.scalar("SELECT count(*) FROM app_user WHERE google_sub = 'test-sub-race'") == 1
    assert (
        env.scalar(
            "SELECT count(*) FROM workspace WHERE owner_user_id ="
            " (SELECT id FROM app_user WHERE google_sub = 'test-sub-race')"
        )
        == 1
    )


# ---------------------------------------------------------------- 로그


async def test_logs_do_not_contain_tokens_codes_or_state(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env.set_identity(ALICE)
    client = env.new_client()
    await env.start(client)
    state, nonce = env.mock.state, env.mock.nonce
    id_token = env.mock.id_token()
    await env.callback(client)  # 성공
    env.mock.edit(nonce="mismatch")
    bad_client = env.new_client()
    await env.start(bad_client)
    await env.callback(bad_client)  # 실패
    session_token = client.cookies.get("orbit_session")
    assert session_token
    # 서버 코드(orbit.*)와 서버가 Google에 보낸 요청의 로그만 본다. 테스트 클라이언트 자신의 요청
    # 로그(콜백 URL의 일회용 code·state)는 서버 로그가 아니다.
    own = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("orbit") or "mock-google.test" in r.getMessage()
    ]
    assert own
    text_logged = "\n".join(own)
    for secret in (
        CODE,
        state,
        nonce,
        id_token,
        session_token,
        CLIENT_SECRET,
        "alice@example.test",
    ):
        assert secret not in text_logged, secret[:8]
