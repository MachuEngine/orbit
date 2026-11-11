"""계정 연결 API·토큰 공급자·Resolver (ADR-0016 §2~§4). 모의 Google, 합성 데이터만 쓴다."""

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from connect_mock import (
    ACCESS_PREFIX,
    CONNECT_REDIRECT_URI,
    REFRESH_TOKEN,
    REVOKE_URL,
    MockGoogleConnect,
)
from google_mock import CLIENT_ID, CLIENT_SECRET, CODE, SIGNING_KEY, attacker_key
from integration.test_google_login import ALICE, BOB, NOW, SUB_PATTERN, Env, _cleanup
from orbit.api.app import create_app
from orbit.connections.oauth import DATA_SCOPES, REQUEST_SCOPES
from orbit.connectors.contracts import PermissionRevoked, TransientError
from orbit.connectors.google.calendar import GoogleCalendarConnector
from orbit.connectors.google.gmail import GmailConnector
from orbit.connectors.google.provider import ConnectionTokenProvider
from orbit.core.clock import FixedClock
from orbit.core.config import Settings
from orbit.core.ids import Uuid7Generator
from orbit.crypto.envelope import (
    EnvelopeCipher,
    KeyEncryptionKey,
    SealedSecret,
    load_kek_from_file,
)
from orbit.runflow.deps import GoogleConnectorResolver, NoConnection

KEK_BYTES = bytes(range(32))
TOKEN_URL = "https://mock-google.test/token"


@pytest.fixture
def owner_engine(owner_url: str) -> Iterator[Engine]:
    engine = create_engine(owner_url, pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "data.key"
    path.write_bytes(KEK_BYTES)
    path.chmod(0o600)
    return path


def _settings(app_url: str, tmp_path: Path, key_file: Path, **extra: Any) -> Settings:
    signing = tmp_path / "signing.key"
    signing.write_text(SIGNING_KEY, encoding="utf-8")
    values: dict[str, Any] = {
        "database_url": SecretStr(app_url),
        "env": "prod",
        "google_client_id": CLIENT_ID,
        "google_client_secret": SecretStr(CLIENT_SECRET),
        "google_redirect_uri": "https://orbit.test/api/auth/google/callback",
        "google_connect_redirect_uri": CONNECT_REDIRECT_URI,
        "session_signing_key_file": str(signing),
        "login_allowed_emails": "alice@example.test, bob@example.test",
        "google_auth_url": "https://mock-google.test/o/oauth2/v2/auth",
        "google_token_url": TOKEN_URL,
        "google_jwks_url": "https://mock-google.test/certs",
        "google_revoke_url": REVOKE_URL,
        "data_key_file": str(key_file),
    }
    values.update(extra)
    return Settings(**values)


@pytest.fixture
async def env(
    app_url: str, owner_engine: Engine, tmp_path: Path, key_file: Path
) -> AsyncIterator[Env]:
    _cleanup(owner_engine)
    clock = FixedClock(NOW)
    ids = Uuid7Generator(clock)
    mock = MockGoogleConnect(clock)
    app = create_app(
        _settings(app_url, tmp_path, key_file),
        clock=clock,
        ids=ids,
        google_transport=mock.transport(),
    )
    async with app.router.lifespan_context(app):
        made = Env(
            clock=clock,
            ids=ids,
            mock=mock,
            transport=httpx.ASGITransport(app=app),
            db=app.state.db,
            owner=owner_engine,
            clients=[],
        )
        try:
            yield made
        finally:
            for client in made.clients:
                await client.aclose()
            _cleanup(owner_engine)


def _mock(env: Env) -> MockGoogleConnect:
    assert isinstance(env.mock, MockGoogleConnect)
    return env.mock


def _csrf(client: httpx.AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("orbit_csrf") or ""}


async def _start(env: Env, client: httpx.AsyncClient) -> httpx.Response:
    response = await client.post("/api/connections/google/start", headers=_csrf(client))
    assert response.status_code == 200, response.text
    env.mock.observe_authorization(response.json()["authorization_url"])
    return response


async def _callback(env: Env, client: httpx.AsyncClient, **extra: str) -> httpx.Response:
    params = {"code": CODE, "state": env.mock.state}
    params.update(extra)
    return await client.get("/api/connections/google/callback", params=params)


async def _connect(env: Env, client: httpx.AsyncClient) -> httpx.Response:
    await _start(env, client)
    return await _callback(env, client)


def _error_of(response: httpx.Response) -> str:
    assert response.status_code == 302
    parts = urlsplit(response.headers["location"])
    assert parts.path == "/"
    return parse_qs(parts.query)["connection_error"][0]


def _per_workspace(env: Env, sql: str, sub: str | None = None) -> list[Any]:
    """합성 사용자들의 워크스페이스마다 `app.workspace_id`를 설정하고 `sql`을 실행한다.

    소유자 역할도 FORCE RLS를 받으므로 워크스페이스 설정 없이는 연결 행이 보이지 않는다.
    """
    out: list[Any] = []
    with env.owner.connect() as conn:
        workspaces = conn.execute(
            text(
                "SELECT w.id FROM workspace w JOIN app_user u ON u.id = w.owner_user_id"
                " WHERE u.google_sub LIKE :p AND (:sub = '' OR u.google_sub = :sub)"
                " ORDER BY w.created_at, w.id"
            ),
            {"p": SUB_PATTERN, "sub": sub or ""},
        ).all()
        for (ws,) in workspaces:
            conn.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws)})
            out.extend(conn.execute(text(sql)).all())
        conn.rollback()
    return out


_ROWS_SQL = (
    "SELECT id, workspace_id, status, account_email, account_sub, scopes, token_ciphertext,"
    " token_nonce, wrapped_dek, kek_id, revoked_at FROM connection ORDER BY created_at, id"
)


def _rows(env: Env, sub: str | None = None) -> list[Any]:
    """합성 사용자(또는 그 `sub`)의 연결 행."""
    return _per_workspace(env, _ROWS_SQL, sub)


def _cipher() -> EnvelopeCipher:
    return EnvelopeCipher(KeyEncryptionKey(KEK_BYTES))


# ---------------------------------------------------------------- 시작


async def test_start_returns_authorization_url_and_signed_cookie(env: Env) -> None:
    client = await env.login(ALICE)
    response = await _start(env, client)
    params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["include_granted_scopes"] == ["true"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [CONNECT_REDIRECT_URI]
    assert set(params["scope"][0].split()) == set(REQUEST_SCOPES)
    cookie = response.headers["set-cookie"].lower()
    for attribute in ("orbit_connect=", "httponly", "samesite=lax", "secure", "max-age=600"):
        assert attribute in cookie
    assert "path=/api/connections/google" in cookie
    assert response.headers["cache-control"] == "no-store"


async def test_start_requires_session_and_csrf(env: Env) -> None:
    anonymous = env.new_client()
    assert (await anonymous.post("/api/connections/google/start")).status_code == 401
    client = await env.login(ALICE)
    assert (await client.post("/api/connections/google/start")).status_code == 403
    assert (await client.get("/api/connections")).status_code == 200
    assert (await anonymous.get("/api/connections")).status_code == 401


# ---------------------------------------------------------------- 정상 연결


async def test_connect_success_lists_active_connection_without_tokens(env: Env) -> None:
    client = await env.login(ALICE)
    response = await _connect(env, client)
    assert (response.status_code, response.headers["location"]) == (302, "/?connection=connected")
    listing = (await client.get("/api/connections")).json()
    assert listing["mode"] == "sim"
    assert listing["connect_available"] is True
    [item] = listing["connections"]
    assert item["status"] == "active"
    assert item["account_email"] == ALICE["email"]
    assert set(DATA_SCOPES) <= set(item["scopes"])
    body = (await client.get("/api/connections")).text
    for secret in (REFRESH_TOKEN, ACCESS_PREFIX, "token_ciphertext", "wrapped_dek"):
        assert secret not in body

    [row] = _rows(env)
    assert row.status == "active"
    assert row.kek_id == _cipher().kek_id
    opened = _cipher().open(
        SealedSecret(
            bytes(row.token_ciphertext), bytes(row.token_nonce), bytes(row.wrapped_dek), row.kek_id
        ),
        connection_id=row.id,
        workspace_id=row.workspace_id,
    )
    assert opened.get_secret_value() == REFRESH_TOKEN


async def test_database_has_no_plaintext_token(env: Env) -> None:
    client = await env.login(ALICE)
    await _connect(env, client)
    # 연결 행 전체와 DB의 다른 사용자 테이블에서 토큰 원문을 바이트로 찾는다.
    needles = [REFRESH_TOKEN.encode(), ACCESS_PREFIX.encode()]
    dumps = [str(r[0]).encode() for r in _per_workspace(env, "SELECT c::text FROM connection c")]
    dumps += [
        bytes(r[0]) + bytes(r[1]) + bytes(r[2])
        for r in _per_workspace(
            env, "SELECT token_ciphertext, token_nonce, wrapped_dek FROM connection"
        )
    ]
    for query in (
        "SELECT t::text FROM auth_session t",
        "SELECT t::text FROM run_message t",
        "SELECT t::text FROM run_event t",
    ):
        dumps += [str(r[0]).encode() for r in _per_workspace(env, query)]
    assert dumps
    for dump in dumps:
        for needle in needles:
            assert needle not in dump


async def test_connect_with_different_google_account_is_allowed(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    env.set_identity(BOB)  # 로그인 사용자와 다른 Google 계정을 연결한다
    response = await _callback(env, client)
    assert response.headers["location"] == "/?connection=connected"
    [item] = (await client.get("/api/connections")).json()["connections"]
    assert item["account_email"] == BOB["email"]


async def test_second_start_while_active_is_conflict(env: Env) -> None:
    client = await env.login(ALICE)
    await _connect(env, client)
    response = await client.post("/api/connections/google/start", headers=_csrf(client))
    assert (response.status_code, response.json()["detail"]) == (409, "already_connected")


# ---------------------------------------------------------------- 실패 경로


async def test_state_mismatch_creates_nothing(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    response = await client.get(
        "/api/connections/google/callback", params={"code": CODE, "state": "wrong-state"}
    )
    assert _error_of(response) == "state"
    assert _rows(env) == []


async def test_callback_without_flow_cookie_is_expired(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    client.cookies.delete("orbit_connect", path="/api/connections/google")
    assert _error_of(await _callback(env, client)) == "expired"
    assert _rows(env) == []


async def test_denied_by_user(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    response = await client.get(
        "/api/connections/google/callback", params={"error": "access_denied"}
    )
    assert _error_of(response) == "denied"


async def test_missing_scope_saves_error_row_and_revokes_grant(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    _mock(env).granted_scope = " ".join(REQUEST_SCOPES[:-1])  # gmail.compose 거부
    assert _error_of(await _callback(env, client)) == "scope_missing"
    [row] = _rows(env)
    assert row.status == "error"
    assert row.token_ciphertext is None and row.kek_id is None
    assert _mock(env).revoke_calls == [REFRESH_TOKEN]  # 쓰지 못하는 권한을 남기지 않는다
    [item] = (await client.get("/api/connections")).json()["connections"]
    assert item["status"] == "error"


async def test_no_refresh_token_saves_error_row(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    _mock(env).include_refresh = False
    assert _error_of(await _callback(env, client)) == "no_refresh_token"
    [row] = _rows(env)
    assert (row.status, row.token_ciphertext) == ("error", None)


async def test_id_token_verification_failure_creates_nothing(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    _mock(env).sign_key = attacker_key()
    assert _error_of(await _callback(env, client)) == "id_token"
    assert _rows(env) == []


async def test_reconnect_replaces_error_connection(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    _mock(env).include_refresh = False
    await _callback(env, client)
    _mock(env).include_refresh = True
    response = await _connect(env, client)
    assert response.headers["location"] == "/?connection=connected"
    rows = _rows(env)
    assert [r.status for r in rows] == ["revoked", "active"]
    [item] = (await client.get("/api/connections")).json()["connections"]
    assert item["status"] == "active"


async def test_connect_unavailable_without_kek_returns_503(
    app_url: str, owner_engine: Engine, tmp_path: Path
) -> None:
    settings = _settings(app_url, tmp_path, tmp_path / "unused", data_key_file=None)
    clock = FixedClock(NOW)
    mock = MockGoogleConnect(clock)
    app = create_app(settings, clock=clock, google_transport=mock.transport())
    async with app.router.lifespan_context(app):
        made = Env(
            clock,
            Uuid7Generator(clock),
            mock,
            httpx.ASGITransport(app=app),
            app.state.db,
            owner_engine,
            [],
        )
        try:
            client = await made.login(ALICE)
            listing = (await client.get("/api/connections")).json()
            assert listing["connect_available"] is False
            response = await client.post("/api/connections/google/start", headers=_csrf(client))
            assert response.status_code == 503
        finally:
            for c in made.clients:
                await c.aclose()
            _cleanup(owner_engine)


# ---------------------------------------------------------------- 격리


async def test_other_workspace_connection_is_404_and_untouched(env: Env) -> None:
    alice = await env.login(ALICE)
    await _connect(env, alice)
    [alice_conn] = (await alice.get("/api/connections")).json()["connections"]
    bob = await env.login(BOB)
    assert (await bob.get("/api/connections")).json()["connections"] == []
    response = await bob.delete(f"/api/connections/{alice_conn['id']}", headers=_csrf(bob))
    assert response.status_code == 404
    assert _mock(env).revoke_calls == []
    [row] = _rows(env, ALICE["sub"])
    assert row.status == "active"
    unknown = await bob.delete(f"/api/connections/{uuid.uuid4()}", headers=_csrf(bob))
    assert unknown.status_code == 404


# ---------------------------------------------------------------- 연결 해제


@pytest.mark.parametrize(
    ("revoke_status", "expected"), [(200, "revoked"), (500, "failed"), (403, "failed")]
)
async def test_disconnect_always_revokes_locally(
    env: Env, revoke_status: int, expected: str
) -> None:
    client = await env.login(ALICE)
    await _connect(env, client)
    [item] = (await client.get("/api/connections")).json()["connections"]
    _mock(env).revoke_status = revoke_status
    response = await client.delete(f"/api/connections/{item['id']}", headers=_csrf(client))
    assert response.status_code == 200
    assert response.json() == {
        "id": item["id"],
        "status": "revoked",
        "provider_revoke": expected,
    }
    assert _mock(env).revoke_calls == [REFRESH_TOKEN]
    [row] = _rows(env)
    assert row.status == "revoked" and row.revoked_at is not None
    assert (row.token_ciphertext, row.token_nonce, row.wrapped_dek, row.kek_id) == (None,) * 4
    assert (await client.get("/api/connections")).json()["connections"] == []


async def test_disconnect_when_google_unreachable_still_revokes_locally(env: Env) -> None:
    client = await env.login(ALICE)
    await _connect(env, client)
    [item] = (await client.get("/api/connections")).json()["connections"]
    _mock(env).network_down = True
    response = await client.delete(f"/api/connections/{item['id']}", headers=_csrf(client))
    assert response.json()["provider_revoke"] == "failed"
    [row] = _rows(env)
    assert (row.status, row.token_ciphertext) == ("revoked", None)


async def test_disconnect_requires_csrf_and_is_idempotent(env: Env) -> None:
    client = await env.login(ALICE)
    await _connect(env, client)
    [item] = (await client.get("/api/connections")).json()["connections"]
    assert (await client.delete(f"/api/connections/{item['id']}")).status_code == 403
    first = await client.delete(f"/api/connections/{item['id']}", headers=_csrf(client))
    second = await client.delete(f"/api/connections/{item['id']}", headers=_csrf(client))
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["provider_revoke"] == "none"
    assert len(_mock(env).revoke_calls) == 1
    # 해제 뒤에는 새 연결을 만들 수 있다.
    again = await _connect(env, client)
    assert again.headers["location"] == "/?connection=connected"


# ---------------------------------------------------------------- 토큰 공급자


async def _active_connection(env: Env) -> tuple[uuid.UUID, uuid.UUID]:
    client = await env.login(ALICE)
    await _connect(env, client)
    [row] = _rows(env)
    return row.workspace_id, row.id


def _provider(env: Env, ws: uuid.UUID, cid: uuid.UUID) -> ConnectionTokenProvider:
    return ConnectionTokenProvider(
        db=env.db,
        cipher=_cipher(),
        clock=env.clock,
        workspace_id=ws,
        connection_id=cid,
        client_id=CLIENT_ID,
        client_secret=SecretStr(CLIENT_SECRET),
        token_url=TOKEN_URL,
        transport=env.mock.transport(),
    )


async def test_provider_caches_and_refreshes_before_expiry(env: Env) -> None:
    ws, cid = await _active_connection(env)
    provider = _provider(env, ws, cid)
    first = await provider.access_token()
    assert first.startswith(ACCESS_PREFIX)
    assert await provider.access_token() == first
    assert _mock(env).refresh_requests == 1
    env.clock.advance(timedelta(seconds=3600 - 61))  # 만료 61초 전: 아직 캐시
    assert await provider.access_token() == first
    env.clock.advance(timedelta(seconds=2))  # 만료 59초 전: 갱신
    second = await provider.access_token()
    assert second != first
    assert _mock(env).refresh_requests == 2
    assert await provider.refresh() not in (first, second)  # 강제 갱신
    assert "SYNTH" not in repr(provider)


async def test_provider_invalid_grant_marks_connection_error(env: Env) -> None:
    ws, cid = await _active_connection(env)
    provider = _provider(env, ws, cid)
    _mock(env).refresh_error = "invalid_grant"
    with pytest.raises(PermissionRevoked):
        await provider.access_token()
    [row] = _rows(env)
    assert row.status == "error"
    assert row.token_ciphertext is None
    with pytest.raises(PermissionRevoked):  # 이제 활성 연결이 아니다
        await provider.refresh()
    assert _mock(env).refresh_requests == 1


async def test_provider_transient_failure_keeps_connection_active(env: Env) -> None:
    ws, cid = await _active_connection(env)
    provider = _provider(env, ws, cid)
    _mock(env).refresh_error = "backend_error"
    _mock(env).refresh_status = 503
    with pytest.raises(TransientError):
        await provider.access_token()
    [row] = _rows(env)
    assert row.status == "active"


async def test_provider_rejects_other_workspace_and_wrong_kek(env: Env) -> None:
    ws, cid = await _active_connection(env)
    other = _provider(env, uuid.uuid4(), cid)  # 다른 워크스페이스에서는 행이 보이지 않는다
    with pytest.raises(PermissionRevoked):
        await other.access_token()
    wrong = _provider(env, ws, cid)
    wrong._cipher = EnvelopeCipher(KeyEncryptionKey(bytes(32)))  # noqa: SLF001
    with pytest.raises(PermissionRevoked):
        await wrong.access_token()


# ---------------------------------------------------------------- Resolver


def _resolver(env: Env) -> GoogleConnectorResolver:
    return GoogleConnectorResolver(
        db=env.db,
        cipher=_cipher(),
        clock=env.clock,
        client_id=CLIENT_ID,
        client_secret=SecretStr(CLIENT_SECRET),
        token_url=TOKEN_URL,
        transport=env.mock.transport(),
    )


async def test_resolver_branches_on_active_connection(env: Env) -> None:
    client = await env.login(ALICE)
    resolver = _resolver(env)
    [ws] = {r[0] for r in _workspaces(env)}
    with pytest.raises(NoConnection):
        await resolver.connectors_for(ws)
    await _connect(env, client)
    calendar, mail = await resolver.connectors_for(ws)
    assert isinstance(calendar, GoogleCalendarConnector)
    assert isinstance(mail, GmailConnector)
    assert (await resolver.connectors_for(ws))[0] is calendar  # 같은 연결이면 재사용
    [item] = (await client.get("/api/connections")).json()["connections"]
    await client.delete(f"/api/connections/{item['id']}", headers=_csrf(client))
    with pytest.raises(NoConnection):
        await resolver.connectors_for(ws)
    other = await env.login(BOB)
    assert other is not None
    bob_ws = next(r[0] for r in _workspaces(env) if r[0] != ws)
    with pytest.raises(NoConnection):  # 다른 워크스페이스의 연결은 쓰지 않는다
        await resolver.connectors_for(bob_ws)
    await resolver.aclose()


def _workspaces(env: Env) -> list[Any]:
    with env.owner.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT w.id FROM workspace w JOIN app_user u ON u.id = w.owner_user_id"
                " WHERE u.google_sub LIKE :p ORDER BY w.created_at"
            ),
            {"p": SUB_PATTERN},
        ).all()
        conn.rollback()
    return list(rows)


# ---------------------------------------------------------------- 로그


async def test_logs_never_contain_tokens(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = await env.login(ALICE)
    await _connect(env, client)
    ws, cid = _rows(env)[0].workspace_id, _rows(env)[0].id
    provider = _provider(env, ws, cid)
    await provider.access_token()
    _mock(env).refresh_error = "invalid_grant"
    with pytest.raises(PermissionRevoked):
        await provider.refresh()
    other = await env.login(BOB)
    await _start(env, other)
    _mock(env).granted_scope = "openid"
    await _callback(env, other)
    text_all = "\n".join(r.getMessage() + str(r.exc_info) for r in caplog.records)
    for secret in (REFRESH_TOKEN, ACCESS_PREFIX, CLIENT_SECRET, SIGNING_KEY):
        assert secret not in text_all


# ---------------------------------------------------------------- 시작 규칙


def test_start_fails_clearly_when_kek_file_is_missing(app_url: str, tmp_path: Path) -> None:
    missing = tmp_path / "absent.key"
    with pytest.raises(RuntimeError) as info:
        create_app(_settings(app_url, tmp_path, missing))
    assert str(missing) in str(info.value)


def test_google_connector_mode_requires_connection_settings(app_url: str, tmp_path: Path) -> None:
    settings = _settings(
        app_url, tmp_path, tmp_path / "x", connector_mode="google", data_key_file=None
    )
    with pytest.raises(RuntimeError) as info:
        create_app(settings)
    assert "ORBIT_DATA_KEY_FILE" in str(info.value)


def test_kek_file_loads_same_id_as_test_cipher(key_file: Path) -> None:
    assert load_kek_from_file(key_file).kek_id == _cipher().kek_id


# ---------------------------------------------------------------- RLS·권한


def _workspace_of(env: Env, sub: str) -> uuid.UUID:
    with env.owner.connect() as conn:
        value = conn.execute(
            text(
                "SELECT w.id FROM workspace w JOIN app_user u ON u.id = w.owner_user_id"
                " WHERE u.google_sub = :sub"
            ),
            {"sub": sub},
        ).scalar_one()
        conn.rollback()
    return uuid.UUID(str(value))


async def test_connection_table_isolation_and_column_privileges(env: Env) -> None:
    alice = await env.login(ALICE)
    await _connect(env, alice)
    await env.login(BOB)
    [alice_row] = _rows(env, ALICE["sub"])
    bob_ws = _workspace_of(env, BOB["sub"])
    insert = text(
        "INSERT INTO connection (id, workspace_id, user_id, provider, account_email, account_sub,"
        " scopes, status, created_at, updated_at) VALUES (:id, :ws, :uid, 'google', 'x', 'y',"
        " ARRAY[]::text[], 'error', :now, :now)"
    )
    async with env.db.workspace_session(bob_ws) as s:
        seen = (await s.execute(text("SELECT count(*) FROM connection"))).scalar_one()
        assert seen == 0  # 다른 워크스페이스의 연결은 보이지 않는다
        updated = await s.execute(
            text("UPDATE connection SET status = 'revoked' WHERE id = :id"),
            {"id": alice_row.id},
        )
        assert updated.rowcount == 0  # type: ignore[attr-defined]
    other_ws = {"id": env.ids.next(), "ws": alice_row.workspace_id, "uid": uuid.uuid4(), "now": NOW}
    with pytest.raises(DBAPIError):  # 다른 워크스페이스 행을 만들 수 없다(RLS WITH CHECK)
        async with env.db.workspace_session(bob_ws) as s:
            await s.execute(insert, other_ws)
    not_member = {"id": env.ids.next(), "ws": bob_ws, "uid": uuid.uuid4(), "now": NOW}
    with pytest.raises(DBAPIError):  # 멤버가 아닌 사용자는 복합 FK가 막는다
        async with env.db.workspace_session(bob_ws) as s:
            await s.execute(insert, not_member)
    with pytest.raises(DBAPIError):  # 계정·범위 열은 UPDATE 권한이 없다
        async with env.db.workspace_session(alice_row.workspace_id) as s:
            await s.execute(
                text("UPDATE connection SET account_email = 'z' WHERE id = :id"),
                {"id": alice_row.id},
            )
