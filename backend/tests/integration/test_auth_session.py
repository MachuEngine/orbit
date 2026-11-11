"""쿠키 세션, CSRF, 세션 조회 함수 격리 (ADR-0013 §2, ADR-0004). 데이터는 모두 합성 값이다."""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text

from integration.runflow_support import Harness
from integration.support import WorkspaceFactory
from orbit.api.app import create_app
from orbit.auth import revoke_session
from orbit.core.config import Settings
from orbit.db.session import AsyncDatabase, SyncDatabase

Make = Callable[..., Awaitable[Harness]]

SCRIPT = [{"text": "안녕하세요."}]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _session_id(h: Harness) -> uuid.UUID:
    info = (await h.client.get("/api/session")).json()
    assert info["authenticated"] is True
    async with h.db.workspace_session(h.ws) as s:
        row = (await s.execute(text("SELECT id FROM auth_session"))).scalar_one()
    return uuid.UUID(str(row))


# ---------------------------------------------------------------- 401


async def test_requests_without_cookie_get_401(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    run_id = await h.start("안녕")
    anonymous = httpx.AsyncClient(transport=h.transport, base_url="http://test")
    async with anonymous:
        calls = [
            ("GET", f"/api/runs/{run_id}"),
            ("GET", f"/api/runs/{run_id}/events"),
            ("GET", f"/api/approvals/{uuid.uuid4()}"),
            ("POST", "/api/runs"),
            ("POST", f"/api/approvals/{uuid.uuid4()}/decision"),
            ("POST", "/api/logout"),
        ]
        for method, path in calls:
            response = await anonymous.request(method, path, json={})
            assert response.status_code == 401, (method, path)
        # 알 수 없는 토큰도 같다
        anonymous.cookies.set("orbit_session", "not-a-session")
        assert (await anonymous.get(f"/api/runs/{run_id}")).status_code == 401
        info = (await anonymous.get("/api/session")).json()
        assert info == {"authenticated": False, "workspace_id": None, "expires_at": None}


async def test_old_workspace_header_no_longer_authenticates(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    anonymous = httpx.AsyncClient(transport=h.transport, base_url="http://test")
    async with anonymous:
        response = await anonymous.post(
            "/api/runs", json={"message": "x"}, headers={"X-Orbit-Workspace": str(h.ws)}
        )
    assert response.status_code == 401


async def test_expired_session_gets_401(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    run_id = await h.start("안녕")
    info = (await h.client.get("/api/session")).json()
    assert (await h.client.get(f"/api/runs/{run_id}")).status_code == 200
    h.clock.advance(timedelta(hours=12) - timedelta(seconds=1))
    assert (await h.client.get(f"/api/runs/{run_id}")).status_code == 200
    h.clock.advance(timedelta(seconds=1))  # 만료 시각 정각부터 무효
    assert info["expires_at"].startswith("2026-11-11T12:00:00")
    assert (await h.client.get(f"/api/runs/{run_id}")).status_code == 401
    assert (await h.client.get("/api/session")).json()["authenticated"] is False


async def test_revoked_session_gets_401(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    run_id = await h.start("안녕")
    await revoke_session(h.db, workspace_id=h.ws, session_id=await _session_id(h), clock=h.clock)
    assert (await h.client.get(f"/api/runs/{run_id}")).status_code == 401
    assert (
        await h.client.post("/api/runs", json={"message": "x"}, headers=h.headers())
    ).status_code == 401


# ---------------------------------------------------------------- CSRF


async def test_csrf_failures_return_403_with_kind_only(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    csrf = h.client.cookies.get("orbit_csrf")
    assert csrf
    body = {"message": "x"}

    missing_header = await h.client.post("/api/runs", json=body)
    assert (missing_header.status_code, missing_header.json()["detail"]) == (403, "csrf_missing")

    wrong_header = await h.client.post("/api/runs", json=body, headers={"X-CSRF-Token": "wrong"})
    assert (wrong_header.status_code, wrong_header.json()["detail"]) == (403, "csrf_mismatch")

    # 헤더와 쿠키가 서로 같아도 세션에 묶인 값이 아니면 거절한다.
    h.client.cookies.set("orbit_csrf", "forged")
    forged = await h.client.post("/api/runs", json=body, headers={"X-CSRF-Token": "forged"})
    assert (forged.status_code, forged.json()["detail"]) == (403, "csrf_mismatch")

    # 쿠키 없이 헤더만 있는 경우
    h.client.cookies.delete("orbit_csrf")
    no_cookie = await h.client.post("/api/runs", json=body, headers={"X-CSRF-Token": csrf})
    assert (no_cookie.status_code, no_cookie.json()["detail"]) == (403, "csrf_missing")

    # 올바른 값으로 복구하면 통과한다. 거절된 요청은 run을 만들지 않았다.
    assert await h.count("SELECT count(*) FROM run") == 0
    h.client.cookies.set("orbit_csrf", csrf)
    assert (
        await h.client.post("/api/runs", json=body, headers={"X-CSRF-Token": csrf})
    ).status_code == 201
    for text_ in (missing_header.text, wrong_header.text, forged.text, no_cookie.text):
        assert csrf not in text_


async def test_csrf_is_required_on_decision_but_not_on_reads(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    approval_id = uuid.uuid4()
    response = await h.client.post(
        f"/api/approvals/{approval_id}/decision", json={"decision": "approve"}
    )
    assert response.status_code == 403
    read = await h.client.get(f"/api/approvals/{approval_id}")
    assert read.status_code == 404  # 읽기는 CSRF 없이 인증만 통과하면 된다


# ---------------------------------------------------------------- 로그인·정상 흐름·로그아웃


async def test_login_sets_cookie_attributes(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    response = await h.client.post("/api/dev/login")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == str(h.ws)
    cookies = {c.split("=", 1)[0]: c.lower() for c in response.headers.get_list("set-cookie")}
    session_cookie, csrf_cookie = cookies["orbit_session"], cookies["orbit_csrf"]
    assert "httponly" in session_cookie
    assert "httponly" not in csrf_cookie
    for cookie in (session_cookie, csrf_cookie):
        assert "samesite=lax" in cookie
        assert "path=/" in cookie
        assert "max-age=43200" in cookie
        assert "secure" not in cookie  # dev 환경의 http


async def test_login_to_decision_flow_with_cookies(make_harness: Make) -> None:
    move = {
        "handle": "E2",
        "start": "2026-11-12T14:00:00+09:00",
        "end": "2026-11-12T15:00:00+09:00",
    }
    day = {"time_min": "2026-11-12T00:00:00+09:00", "time_max": "2026-11-13T00:00:00+09:00"}
    script = [
        {"text": "찾아본다.", "tool_calls": [{"name": "list_events", "arguments": day}]},
        {"text": None, "tool_calls": [{"name": "update_event_time", "arguments": move}]},
        {"text": "옮겼습니다."},
    ]
    h = await make_harness(script)
    assert (await h.client.get("/api/session")).json()["workspace_id"] == str(h.ws)
    assert (await h.client.get("/api/healthz")).json() == {"status": "ok"}

    run_id = await h.start("점심을 2시로 옮겨줘")
    await h.work()
    assert await h.status(run_id) == "waiting_approval"
    approval_id = await h.approval_id(run_id)
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    stream = await h.client.get(f"/api/runs/{run_id}/events")  # 쿠키만으로 SSE 인증
    assert stream.status_code == 200
    assert stream.text.startswith("id: 1\n")
    assert await h.status(run_id) == "succeeded"


async def test_logout_revokes_session_and_clears_cookies(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    run_id = await h.start("안녕")

    no_csrf = await h.client.post("/api/logout")
    assert no_csrf.status_code == 403
    assert (await h.client.get(f"/api/runs/{run_id}")).status_code == 200  # 아직 유효

    stale_token = h.client.cookies.get("orbit_session")
    out = await h.client.post("/api/logout", headers=h.headers())
    assert out.status_code == 204
    set_cookies = [c.lower() for c in out.headers.get_list("set-cookie")]
    assert any(c.startswith("orbit_session=") and "max-age=0" in c for c in set_cookies)
    assert any(c.startswith("orbit_csrf=") and "max-age=0" in c for c in set_cookies)

    assert (await h.client.get(f"/api/runs/{run_id}")).status_code == 401
    # 쿠키를 다시 붙여도 서버에서 폐기돼 있다.
    assert stale_token
    h.client.cookies.set("orbit_session", stale_token)
    assert (await h.client.get(f"/api/runs/{run_id}")).status_code == 401
    assert (await h.client.get("/api/session")).json()["authenticated"] is False


async def _dev_login_status(app_url: str, dev_workspace: uuid.UUID | None) -> int:
    settings = Settings(database_url=SecretStr(app_url), env="dev", dev_workspace_id=dev_workspace)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/dev/login")
            assert "orbit_session" not in response.headers.get("set-cookie", "")
            return response.status_code


async def test_dev_login_does_not_create_workspace(app_url: str, async_db: AsyncDatabase) -> None:
    missing = uuid.uuid4()
    assert await _dev_login_status(app_url, missing) == 404
    assert await _dev_login_status(app_url, None) == 404
    async with async_db.workspace_session(missing) as s:
        count = (await s.execute(text("SELECT count(*) FROM workspace"))).scalar_one()
    assert count == 0


def test_app_refuses_to_start_outside_dev(app_url: str) -> None:
    with pytest.raises(RuntimeError):
        create_app(Settings(database_url=SecretStr(app_url), env="prod"))


# ---------------------------------------------------------------- DB 저장 형태와 조회 함수


async def test_token_plaintext_is_not_stored(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    token = h.client.cookies.get("orbit_session")
    csrf = h.client.cookies.get("orbit_csrf")
    assert token and csrf
    async with h.db.workspace_session(h.ws) as s:
        rows = (await s.execute(text("SELECT * FROM auth_session"))).all()
    assert len(rows) == 1
    dumped = " ".join(str(v) for v in rows[0])
    assert token not in dumped
    assert csrf not in dumped
    mapping = rows[0]._mapping
    assert mapping["token_hash"] == _sha(token)
    assert mapping["csrf_token_hash"] == _sha(csrf)


async def test_lookup_function_exposes_only_the_row_for_the_token_hash(
    make_harness: Make,
    workspaces: WorkspaceFactory,
    raw_engine: Engine,
    sync_db: SyncDatabase,
) -> None:
    h = await make_harness(SCRIPT)
    other = await h.client_for(workspaces.create())
    mine_token = h.client.cookies.get("orbit_session")
    other_token = other.cookies.get("orbit_session")
    mine_csrf = h.client.cookies.get("orbit_csrf")
    assert mine_token and other_token and mine_csrf
    session_id = str(await _session_id(h))

    def lookup(value: str) -> list[tuple[object, ...]]:
        with raw_engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM auth_session_lookup(:v)"), {"v": value}).all()
            conn.rollback()
        return [tuple(r) for r in rows]

    found = lookup(_sha(mine_token))
    assert len(found) == 1
    assert str(found[0][0]) == session_id
    assert str(found[0][1]) == str(h.ws)
    assert len(lookup(_sha(other_token))) == 1

    # 해시가 아닌 입력(원문 토큰, CSRF 해시, 세션 ID, 패턴, 빈 값)은 어떤 행도 노출하지 않는다.
    for value in (
        mine_token,
        mine_csrf,
        _sha(mine_csrf),
        session_id,
        str(h.ws),
        "",
        "%",
        "' OR '1'='1",
        _sha(mine_token).upper(),
        _sha(mine_token)[:-1],
    ):
        assert lookup(value) == [], value

    # 함수 밖에서는 RLS가 그대로 적용된다: 워크스페이스 설정이 없거나 다르면 테이블이 비어 보인다.
    with raw_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM auth_session")).scalar_one() == 0
        conn.rollback()
    with sync_db.workspace_session(uuid.uuid4()) as s:
        assert s.execute(text("SELECT count(*) FROM auth_session")).scalar_one() == 0
    with sync_db.workspace_session(h.ws) as s:
        assert s.execute(text("SELECT count(*) FROM auth_session")).scalar_one() == 1
