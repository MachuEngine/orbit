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
from orbit.connectors.contracts import (
    ConnectorError,
    FileQuery,
    PermissionRevoked,
    TransientError,
)
from orbit.connectors.google.calendar import GoogleCalendarConnector
from orbit.connectors.google.drive import GoogleDriveConnector
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
from orbit.mailindex import store as mail_index_store
from orbit.runflow.deps import GoogleConnectorResolver, NoConnection, ScopeMissingDrive

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


# ---------------------------------------------------------------- 철회 범위 (두 탭 경쟁)


async def test_losing_tab_does_not_revoke_the_active_connection(env: Env) -> None:
    first = await env.login(ALICE)
    await _start(env, first)
    saved = (env.mock.state, env.mock.nonce, env.mock.challenge)
    second = await env.login(ALICE)  # 같은 사용자의 다른 탭(다른 세션, 같은 워크스페이스)
    await _connect(env, second)
    assert [r.status for r in _rows(env)] == ["active"]
    env.mock.state, env.mock.nonce, env.mock.challenge = saved
    # 먼저 시작한 탭의 콜백이 늦게 도착한다. 새 토큰은 버리고 철회하지 않는다.
    assert _error_of(await _callback(env, first)) == "already_connected"
    assert _mock(env).revoke_calls == []
    [row] = _rows(env)
    assert row.status == "active" and row.token_ciphertext is not None


async def test_scope_missing_does_not_revoke_when_same_account_is_active(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    saved = (env.mock.state, env.mock.nonce, env.mock.challenge)
    other = await env.login(ALICE)
    await _connect(env, other)
    env.mock.state, env.mock.nonce, env.mock.challenge = saved
    _mock(env).granted_scope = " ".join(REQUEST_SCOPES[:-1])
    assert _error_of(await _callback(env, client)) == "already_connected"
    assert _mock(env).revoke_calls == []
    assert [r.status for r in _rows(env)] == ["active"]


async def test_new_token_is_revoked_when_active_connection_is_another_account(env: Env) -> None:
    first = await env.login(ALICE)
    await _start(env, first)
    saved = (env.mock.state, env.mock.nonce, env.mock.challenge)
    other = await env.login(ALICE)
    await _connect(env, other)  # alice 계정으로 활성 연결
    env.mock.state, env.mock.nonce, env.mock.challenge = saved
    env.set_identity(BOB)  # 늦게 도착한 콜백은 다른 Google 계정의 토큰이다
    _mock(env).refresh_token = "test-refresh-token-SYNTH-bob"
    assert _error_of(await _callback(env, first)) == "already_connected"
    assert _mock(env).revoke_calls == ["test-refresh-token-SYNTH-bob"]
    assert [r.status for r in _rows(env)] == ["active"]


def test_prod_start_rejects_wide_kek_permissions(app_url: str, tmp_path: Path) -> None:
    wide = tmp_path / "wide.key"
    wide.write_bytes(KEK_BYTES)
    wide.chmod(0o644)
    with pytest.raises(RuntimeError) as info:
        create_app(_settings(app_url, tmp_path, wide))
    assert str(wide) in str(info.value)
    create_app(_settings(app_url, tmp_path, wide, env="dev"))  # dev는 경고만 한다


# ---------------------------------------------------------------- 문서 권한 추가 (ADR-0020 §1)

DRIVE = "https://www.googleapis.com/auth/drive.readonly"
NEW_REFRESH = "test-refresh-token-SYNTH-0002"


async def _docs_start(env: Env, client: httpx.AsyncClient, connection_id: str) -> httpx.Response:
    response = await client.post(
        f"/api/connections/{connection_id}/scopes/docs/start", headers=_csrf(client)
    )
    if response.status_code == 200:
        env.mock.observe_authorization(response.json()["authorization_url"])
    return response


async def _connected(env: Env, who: dict[str, str] = ALICE) -> tuple[httpx.AsyncClient, str]:
    client = await env.login(who)
    await _connect(env, client)
    [item] = (await client.get("/api/connections")).json()["connections"]
    return client, item["id"]


def _grant_docs(env: Env) -> None:
    _mock(env).granted_scope = " ".join((*REQUEST_SCOPES, DRIVE))
    _mock(env).refresh_token = NEW_REFRESH


async def test_connection_list_reports_docs_permission(env: Env) -> None:
    client, cid = await _connected(env)
    [item] = (await client.get("/api/connections")).json()["connections"]
    assert item["docs_granted"] is False
    assert DRIVE not in item["scopes"]  # 첫 연결에는 문서 범위를 요청하지 않았다
    assert DRIVE not in REQUEST_SCOPES
    _grant_docs(env)
    await _docs_start(env, client, cid)
    await _callback(env, client)
    [item] = (await client.get("/api/connections")).json()["connections"]
    assert item["docs_granted"] is True and DRIVE in item["scopes"]


async def test_docs_start_asks_only_for_drive_with_incremental_consent(env: Env) -> None:
    client, cid = await _connected(env)
    response = await _docs_start(env, client, cid)
    params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert set(params["scope"][0].split()) == {"openid", "email", DRIVE}
    assert params["include_granted_scopes"] == ["true"]
    assert params["access_type"] == ["offline"] and params["prompt"] == ["consent"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [CONNECT_REDIRECT_URI]
    assert params["login_hint"] == [ALICE["email"]]
    cookie = response.headers["set-cookie"].lower()
    for attribute in ("orbit_connect=", "httponly", "samesite=lax", "secure", "max-age=600"):
        assert attribute in cookie
    assert "path=/api/connections/google" in cookie
    assert response.headers["cache-control"] == "no-store"


async def test_docs_start_requires_session_and_csrf(env: Env) -> None:
    client, cid = await _connected(env)
    path = f"/api/connections/{cid}/scopes/docs/start"
    assert (await env.new_client().post(path)).status_code == 401
    assert (await client.post(path)).status_code == 403  # CSRF 헤더 없음
    bad = await client.post(path, headers={"X-CSRF-Token": "wrong"})
    assert bad.status_code == 403


async def test_docs_add_updates_the_same_row_keeps_index_and_old_token(env: Env) -> None:
    client, cid = await _connected(env)
    [before] = _rows(env)
    ws = before.workspace_id
    async with env.db.workspace_session(ws) as s:  # 메일 색인 상태(ADR-0019)
        await mail_index_store.ensure_sync_state(s, ws, before.id, env.clock.now())
    _grant_docs(env)
    await _docs_start(env, client, cid)
    response = await _callback(env, client)
    assert (response.status_code, response.headers["location"]) == (302, "/?connection=docs_added")
    [after] = _rows(env)  # 새 행이 생기지 않았다
    assert after.id == before.id == uuid.UUID(cid) and after.status == "active"
    assert DRIVE in after.scopes and set(DATA_SCOPES) <= set(after.scopes)
    opened = _cipher().open(
        SealedSecret(
            bytes(after.token_ciphertext),
            bytes(after.token_nonce),
            bytes(after.wrapped_dek),
            after.kek_id,
        ),
        connection_id=after.id,
        workspace_id=after.workspace_id,
    )
    assert opened.get_secret_value() == NEW_REFRESH  # 암호문이 새 토큰으로 바뀌었다
    assert _mock(env).revoke_calls == []  # 이전 토큰을 철회하지 않는다
    assert _per_workspace(env, "SELECT connection_id FROM sync_state") == [(before.id,)]
    body = (await client.get("/api/connections")).text
    for secret in (REFRESH_TOKEN, NEW_REFRESH, ACCESS_PREFIX):
        assert secret not in body


async def test_docs_add_with_a_different_account_is_rejected(env: Env) -> None:
    client, cid = await _connected(env)
    [before] = _rows(env)
    _grant_docs(env)
    await _docs_start(env, client, cid)
    env.set_identity(BOB)  # 다른 Google 계정으로 동의했다
    assert _error_of(await _callback(env, client)) == "account_mismatch"
    [after] = _rows(env)
    assert (after.scopes, after.token_ciphertext) == (before.scopes, before.token_ciphertext)
    assert after.status == "active"
    # 쓰지 못할 다른 계정의 새 토큰만 철회하고 기존 연결 토큰은 건드리지 않는다.
    assert _mock(env).revoke_calls == [NEW_REFRESH]


async def test_docs_add_without_the_drive_scope_keeps_the_connection(env: Env) -> None:
    client, cid = await _connected(env)
    [before] = _rows(env)
    _mock(env).refresh_token = NEW_REFRESH
    await _docs_start(env, client, cid)  # 모의 Google은 기본 범위(문서 제외)만 허용한다
    assert _error_of(await _callback(env, client)) == "docs_scope_missing"
    [after] = _rows(env)
    assert (after.scopes, after.token_ciphertext) == (before.scopes, before.token_ciphertext)
    assert _mock(env).revoke_calls == []  # 같은 계정의 활성 연결이 있으면 새 토큰을 철회하지 않는다


async def test_docs_add_without_refresh_token_keeps_the_connection(env: Env) -> None:
    client, cid = await _connected(env)
    [before] = _rows(env)
    _grant_docs(env)
    _mock(env).include_refresh = False
    await _docs_start(env, client, cid)
    assert _error_of(await _callback(env, client)) == "no_refresh_token"
    [after] = _rows(env)
    assert (after.scopes, after.token_ciphertext) == (before.scopes, before.token_ciphertext)


async def test_docs_add_after_disconnect_is_rejected(env: Env) -> None:
    client, cid = await _connected(env)
    _grant_docs(env)
    await _docs_start(env, client, cid)
    await client.delete(f"/api/connections/{cid}", headers=_csrf(client))
    assert _error_of(await _callback(env, client)) == "connection_inactive"
    [row] = _rows(env)
    assert row.status == "revoked" and row.token_ciphertext is None
    assert _mock(env).revoke_calls[-1] == NEW_REFRESH  # 쓸 곳이 없는 새 토큰은 철회한다


async def test_docs_add_state_mismatch_and_tampered_cookie_change_nothing(env: Env) -> None:
    client, cid = await _connected(env)
    [before] = _rows(env)
    _grant_docs(env)
    await _docs_start(env, client, cid)
    wrong = await client.get(
        "/api/connections/google/callback", params={"code": CODE, "state": "wrong-state"}
    )
    assert _error_of(wrong) == "state"
    cookie = client.cookies.get("orbit_connect") or ""
    forged = cookie.replace(".", "x.", 1)  # 서명이 깨진다
    client.cookies.set("orbit_connect", forged, path="/api/connections/google")
    assert _error_of(await _callback(env, client)) == "expired"
    [after] = _rows(env)
    assert (after.scopes, after.token_ciphertext) == (before.scopes, before.token_ciphertext)
    assert _mock(env).revoke_calls == []


async def test_docs_start_conflicts(env: Env) -> None:
    client, cid = await _connected(env)
    unknown = await _docs_start(env, client, str(uuid.uuid4()))
    assert unknown.status_code == 404
    _grant_docs(env)
    await _docs_start(env, client, cid)
    await _callback(env, client)
    again = await _docs_start(env, client, cid)
    assert (again.status_code, again.json()["detail"]) == (409, "docs_already_granted")


async def test_docs_start_for_error_connection_is_conflict(env: Env) -> None:
    client = await env.login(ALICE)
    await _start(env, client)
    _mock(env).include_refresh = False
    await _callback(env, client)  # error 행
    [row] = _rows(env)
    response = await _docs_start(env, client, str(row.id))
    assert (response.status_code, response.json()["detail"]) == (409, "connection_inactive")


async def test_docs_add_is_isolated_between_workspaces(env: Env) -> None:
    alice, alice_cid = await _connected(env)
    [alice_row] = _rows(env, ALICE["sub"])
    bob = await env.login(BOB)
    assert (await _docs_start(env, bob, alice_cid)).status_code == 404
    # Alice의 서명 쿠키를 Bob이 가로채 Bob 세션으로 콜백해도 Alice의 연결은 바뀌지 않는다.
    _grant_docs(env)
    await _docs_start(env, alice, alice_cid)
    stolen = alice.cookies.get("orbit_connect") or ""
    bob.cookies.set("orbit_connect", stolen, path="/api/connections/google")
    env.set_identity(ALICE)
    assert _error_of(await _callback(env, bob)) == "connection_inactive"
    [after] = _rows(env, ALICE["sub"])
    assert (after.scopes, after.token_ciphertext) == (
        alice_row.scopes,
        alice_row.token_ciphertext,
    )


async def test_resolver_gives_drive_only_with_the_scope_and_a_fresh_token(env: Env) -> None:
    client, cid = await _connected(env)
    resolver = _resolver(env)
    [ws] = {r[0] for r in _workspaces(env)}
    stub = await resolver.drive_for(ws)
    assert isinstance(stub, ScopeMissingDrive)
    with pytest.raises(ConnectorError) as caught:
        await stub.search_files(FileQuery())
    assert caught.value.reason == "scope_missing"
    assert _mock(env).refresh_requests == 0  # 권한이 없으면 토큰도 받지 않는다
    _grant_docs(env)
    await _docs_start(env, client, cid)
    await _callback(env, client)
    drive = await resolver.drive_for(ws)
    assert isinstance(drive, GoogleDriveConnector)
    assert await resolver.drive_for(ws) is drive  # 같은 연결이면 재사용
    await client.delete(f"/api/connections/{cid}", headers=_csrf(client))
    with pytest.raises(NoConnection):
        await resolver.drive_for(ws)
    await resolver.aclose()


async def test_concurrent_drive_for_builds_one_connector(env: Env) -> None:
    import asyncio

    client, cid = await _connected(env)
    _grant_docs(env)
    await _docs_start(env, client, cid)
    await _callback(env, client)
    resolver = _resolver(env)
    [ws] = {r[0] for r in _workspaces(env)}
    found = await asyncio.gather(*(resolver.drive_for(ws) for _ in range(8)))
    assert len({id(d) for d in found}) == 1 and isinstance(found[0], GoogleDriveConnector)
    assert len(resolver._drive) == 1  # noqa: SLF001
    await resolver.aclose()
