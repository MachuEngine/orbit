"""7단계 정리: 커넥터 오류 알림, 실패 로그, 멱등 키, SSE 세션 재확인. 데이터는 합성 값이다."""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from integration.runflow_support import ACCOUNT, DAY, FlakyCalendar, Harness, StubResolver
from orbit.connectors.contracts import ConnectorError, RateLimited, TransientError

Make = Callable[..., Awaitable[Harness]]
FORBIDDEN_WORDS = ("점심을 2시로", "SYNTH-BODY-MARK", "orbit_session", "@example")


class _ErrorCalendar(FlakyCalendar):
    def __init__(self, inner: Any, error: ConnectorError) -> None:
        super().__init__(inner)
        self._error = error

    async def list_calendars(self) -> list[Any]:
        raise self._error


def _fail_calendar(h: Harness, error: ConnectorError) -> None:
    h.deps.resolver = StubResolver(
        _ErrorCalendar(h.conns.calendar[ACCOUNT], error), h.conns.mail[ACCOUNT]
    )


# ---------------------------------------------------------------- connector_notice


async def test_connector_error_emits_one_notice_per_step_service_kind(
    make_harness: Make, caplog: pytest.LogCaptureFixture
) -> None:
    script = [
        {
            "tool_calls": [
                {"name": "list_events", "arguments": DAY},
                {"name": "list_events", "arguments": DAY},
            ]
        },
        {"text": "일정을 가져오지 못했습니다."},
    ]
    h = await make_harness(script)
    _fail_calendar(h, ConnectorError("합성 오류", reason="accessNotConfigured"))
    caplog.set_level(logging.INFO)
    run_id = await h.start("내일 일정 알려줘")
    await h.work()

    assert await h.status(run_id) == "succeeded"
    notices = [e for e in await h.events(run_id) if e.type == "connector_notice"]
    assert [e.payload for e in notices] == [
        {
            "step": 0,
            "service": "calendar",
            "kind": "service_disabled",
            "reason": "accessNotConfigured",
        }
    ]
    # 모델에 가는 도구 결과는 종류만 담고 사유 코드는 담지 않는다.
    async with h.db.workspace_session(h.ws) as s:
        rows = (
            await s.execute(text("SELECT content->>'text' FROM run_message WHERE role = 'tool'"))
        ).all()
    assert {r[0] for r in rows} == {'{"error": "service_disabled"}'}
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert f"run_id={run_id}" in logged
    assert "service=calendar kind=service_disabled reason=accessNotConfigured" in logged


async def test_rate_limited_and_transient_notices(make_harness: Make) -> None:
    for error, kind in (
        (RateLimited(reason="rateLimitExceeded"), "rate_limited"),
        (TransientError("합성"), "transient_error"),
    ):
        h = await make_harness(
            [{"tool_calls": [{"name": "list_events", "arguments": DAY}]}, {"text": "실패"}]
        )
        _fail_calendar(h, error)
        run_id = await h.start("일정")
        await h.work()
        notices = [e.payload for e in await h.events(run_id) if e.type == "connector_notice"]
        assert [(n["service"], n["kind"]) for n in notices] == [("calendar", kind)]


async def test_non_connector_origin_errors_have_no_notice(make_harness: Make) -> None:
    script = [
        {"tool_calls": [{"name": "get_event", "arguments": {"handle": "E99"}}]},
        {"text": "없음"},
    ]
    h = await make_harness(script)
    run_id = await h.start("일정")
    await h.work()
    assert "connector_notice" not in await h.event_types(run_id)


# ---------------------------------------------------------------- 실패 로그


class _NoConnection:
    async def connectors_for(self, workspace_id: uuid.UUID) -> Any:
        from orbit.runflow.deps import NoConnection

        raise NoConnection(f"workspace_id={workspace_id}")

    async def drive_for(self, workspace_id: uuid.UUID) -> Any:
        from orbit.runflow.deps import NoConnection

        raise NoConnection(f"workspace_id={workspace_id}")


async def test_failed_run_logs_run_id_and_error_kind_only(
    make_harness: Make, caplog: pytest.LogCaptureFixture
) -> None:
    h = await make_harness([{"tool_calls": [{"name": "list_events", "arguments": DAY}]}])
    h.deps.resolver = _NoConnection()
    caplog.set_level(logging.INFO)
    run_id = await h.start("점심을 2시로 옮겨줘")
    await h.work()

    assert await h.status(run_id) == "failed"
    messages = [r.getMessage() for r in caplog.records if r.name == "orbit.runflow.nodes"]
    assert any(f"run_id={run_id}" in m and "error_kind=no_connection" in m for m in messages)
    everything = "\n".join(r.getMessage() + str(r.exc_info) for r in caplog.records)
    for word in FORBIDDEN_WORDS:
        assert word not in everything


# ---------------------------------------------------------------- 멱등 키


def _key() -> str:
    return str(uuid.uuid4())


async def test_same_key_replays_without_new_run_or_job(make_harness: Make) -> None:
    h = await make_harness([{"text": "안녕"}])
    key = _key()
    headers = {**h.headers(), "Idempotency-Key": key}
    first = await h.client.post("/api/runs", json={"message": "안녕"}, headers=headers)
    second = await h.client.post("/api/runs", json={"message": "안녕"}, headers=headers)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert await h.count("SELECT count(*) FROM run") == 1
    assert await h.count("SELECT count(*) FROM conversation") == 1
    assert await h.count("SELECT count(*) FROM run_message") == 1
    assert len(await h.jobs("execute_run")) == 1


async def test_same_key_with_other_body_is_409(make_harness: Make) -> None:
    h = await make_harness([{"text": "x"}])
    headers = {**h.headers(), "Idempotency-Key": _key()}
    assert (
        await h.client.post("/api/runs", json={"message": "하나"}, headers=headers)
    ).status_code == 201
    other = await h.client.post("/api/runs", json={"message": "둘"}, headers=headers)
    assert other.status_code == 409
    assert other.json()["detail"] == "idempotency_key_reused"
    assert await h.count("SELECT count(*) FROM run") == 1


async def test_concurrent_same_key_creates_one_run(make_harness: Make) -> None:
    h = await make_harness([{"text": "x"}])
    headers = {**h.headers(), "Idempotency-Key": _key()}
    results = await asyncio.gather(
        *(h.client.post("/api/runs", json={"message": "동시"}, headers=headers) for _ in range(4))
    )
    assert sorted(r.status_code for r in results) == [200, 200, 200, 201]
    assert len({r.json()["run_id"] for r in results}) == 1
    assert await h.count("SELECT count(*) FROM run") == 1
    assert len(await h.jobs("execute_run")) == 1


async def test_same_key_in_other_workspace_is_independent(
    make_harness: Make, workspaces: Any
) -> None:
    h = await make_harness([{"text": "x"}])
    other_ws = workspaces.create()
    other = await h.client_for(other_ws)
    key = _key()
    first = await h.client.post(
        "/api/runs", json={"message": "같다"}, headers={**h.headers(), "Idempotency-Key": key}
    )
    second = await other.post(
        "/api/runs",
        json={"message": "같다"},
        headers={**h.headers(other), "Idempotency-Key": key},
    )
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["run_id"] != second.json()["run_id"]


async def test_invalid_key_is_400_and_missing_key_keeps_old_behavior(make_harness: Make) -> None:
    h = await make_harness([{"text": "x"}])
    bad = await h.client.post(
        "/api/runs", json={"message": "a"}, headers={**h.headers(), "Idempotency-Key": "not-uuid"}
    )
    assert bad.status_code == 400
    assert await h.count("SELECT count(*) FROM run") == 0
    for _ in range(2):  # 키가 없으면 매번 새 run이다
        assert (
            await h.client.post("/api/runs", json={"message": "a"}, headers=h.headers())
        ).status_code == 201
    assert await h.count("SELECT count(*) FROM run") == 2


# ---------------------------------------------------------------- SSE 세션 재확인


async def _waiting_stream(h: Harness) -> tuple[uuid.UUID, "asyncio.Task[Any]"]:
    run_id = await h.start("일정")  # 작업을 돌리지 않아 queued로 남는다(종료 상태가 아니다)
    stream = asyncio.create_task(h.client.get(f"/api/runs/{run_id}/events"))
    await asyncio.sleep(0.6)
    assert not stream.done()
    return run_id, stream


async def test_sse_closes_after_logout(make_harness: Make) -> None:
    h = await make_harness([{"text": "x"}])
    _, stream = await _waiting_stream(h)
    assert (await h.client.post("/api/logout", headers=h.headers())).status_code == 204
    # 재확인 주기가 지나기 전에는 열려 있고, 주기가 지나면 닫힌다.
    await asyncio.sleep(0.6)
    assert not stream.done()
    h.api_clock.advance(timedelta(seconds=3))
    response = await asyncio.wait_for(stream, timeout=5)
    assert response.status_code == 200
    assert "orbit_session" not in response.text


async def test_sse_closes_after_session_expiry(make_harness: Make) -> None:
    h = await make_harness([{"text": "x"}])
    _, stream = await _waiting_stream(h)
    h.api_clock.advance(timedelta(days=2))
    response = await asyncio.wait_for(stream, timeout=5)
    assert response.status_code == 200
