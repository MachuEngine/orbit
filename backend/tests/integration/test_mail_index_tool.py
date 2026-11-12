"""색인 메일 검색 도구와 질의 직전 동기화 (ADR-0019 §6·§8). 데이터는 모두 합성 값이다."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from integration.mailindex_support import ACCOUNT, IndexEnv, add_inbox, create_connection, mail
from integration.runflow_support import BODY_MARKER, Harness
from orbit.assistant.tools import ReadOnlyTools, ToolOutcome
from orbit.assistant.workset import WorkSet
from orbit.connectors.contracts import MailConnector, PermissionRevoked
from orbit.mailindex import service
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import EmbeddingError, FakeEmbedder
from orbit.mailindex.sync import sync_connection
from orbit.runflow.index_tool import SEARCH_INDEXED_MAIL, IndexedMailSearch

TZ = ZoneInfo("Asia/Seoul")
Make = Callable[..., Awaitable[Harness]]


async def run_tool(
    ix: IndexEnv,
    arguments: object,
    *,
    workset: WorkSet | None = None,
    mail_connector: MailConnector | None = None,
) -> tuple[ToolOutcome, dict[str, Any]]:
    outcome = await IndexedMailSearch(ix.mi, TZ).execute(
        arguments,
        workspace_id=ix.ws,
        workset=workset or WorkSet(),
        mail=mail_connector or ix.mail,
    )
    return outcome, json.loads(outcome.content)


def history_calls(ix: IndexEnv) -> int:
    return sum(1 for r in ix.conns.calls.records if r.tool == "list_history")


class SlowHistory:
    """`list_history`만 오래 걸리게 한 메일 커넥터."""

    def __init__(self, inner: MailConnector, delay_s: float) -> None:
        self._inner = inner
        self._delay = delay_s

    async def list_history(self, *a: Any, **k: Any) -> Any:
        await asyncio.sleep(self._delay)
        return await self._inner.list_history(*a, **k)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_index_empty_when_no_scope_and_pending_before_first_sync(ix: IndexEnv) -> None:
    outcome, content = await run_tool(ix, {"query": "견적"})
    assert content == {"index_empty": True, "results": []} and not outcome.is_error
    await add_inbox(ix)
    _, content = await run_tool(ix, {"query": "견적"})
    assert content["results"] == [] and content["index_pending"] is True
    assert (
        content["indexed_as_of"] is None and history_calls(ix) == 0
    )  # 질의 경로는 첫 동기화를 하지 않는다


async def test_results_register_handles_and_read_mail_follows(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    workset = WorkSet()
    outcome, content = await run_tool(
        ix, {"query": "견적서 납기 협의", "max_results": 3}, workset=workset
    )
    assert not outcome.is_error and 1 <= len(content["results"]) <= 3
    first = content["results"][0]
    assert first["handle"] == "M1" and first["subject"] == "견적서 납기 협의"
    assert first["from"] == {"name": "합성 발신자", "email": "bob@example.org"}
    assert first["date"].endswith("+09:00") and "견적서" in first["excerpt"]
    assert first["excerpt_truncated"] is False
    assert content["indexed_as_of"] == "2026-11-11T18:00:00+09:00"
    assert content["indexed_minutes_ago"] == 0
    # 제공자 ID는 결과에 없다. 핸들은 서버 쪽 작업 집합에만 매핑돼 있다.
    assert "m-quote" not in outcome.content and "thr-" not in outcome.content
    assert workset.resolve_message("M1") == "m-quote"
    # 같은 메일은 다시 검색해도 같은 핸들이다.
    _, again = await run_tool(ix, {"query": "견적서"}, workset=workset)
    assert again["results"][0]["handle"] == "M1"
    # read_mail로 이어서 읽는다.
    reader = ReadOnlyTools(
        calendar=ix.conns.calendar[ACCOUNT], mail=ix.mail, tz=TZ, workset=workset
    )
    read = json.loads((await reader.execute("read_mail", {"handle": "M1"})).content)
    assert read["subject"] == "견적서 납기 협의" and "금요일" in read["body"]


async def test_invalid_arguments_are_rejected_without_searching(ix: IndexEnv) -> None:
    await add_inbox(ix)
    for bad in (None, {}, {"query": ""}, {"query": "a", "max_results": 99}, {"query": "a", "x": 1}):
        outcome, content = await run_tool(ix, bad)
        assert outcome.is_error and content == {"error": "invalid_arguments"}


async def test_fresh_index_is_searched_without_syncing(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    before = history_calls(ix)
    ix.clock.advance(timedelta(minutes=4))  # 5분 이내
    await run_tool(ix, {"query": "견적서"})
    assert history_calls(ix) == before


async def test_stale_index_is_synced_incrementally_before_searching(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    ix.state.add_message(mail("m-fresh", "방금 온 계약서", "방금 도착한 합성 계약서 보라색코끼리"))
    ix.clock.advance(timedelta(minutes=6))  # 5분 초과
    before = history_calls(ix)
    _, content = await run_tool(ix, {"query": "보라색코끼리"})
    assert history_calls(ix) == before + 1
    assert [r["subject"] for r in content["results"][:1]] == ["방금 온 계약서"]
    assert content["indexed_minutes_ago"] == 0  # 동기화로 기준 시각이 갱신됐다


async def test_slow_sync_is_cut_off_and_search_uses_the_existing_index(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    ix.state.add_message(mail("m-late", "늦게 온 메일", "늦게 도착한 합성 본문 보라색코끼리"))
    ix.clock.advance(timedelta(minutes=6))
    ix.mi.query_sync_timeout_s = 0.05
    started = time.monotonic()
    _, content = await run_tool(
        ix, {"query": "견적서 납기"}, mail_connector=SlowHistory(ix.mail, delay_s=2.0)
    )
    assert time.monotonic() - started < 1.5  # 느린 동기화를 기다리지 않았다
    assert content["results"] and content["indexed_minutes_ago"] == 6  # 기존 색인 기준이다
    assert "m-late" not in {*(await ix.item_ids())}
    # 취소된 동기화가 잠금을 남기지 않는다.
    assert (await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)).outcome == "synced"
    assert "m-late" in await ix.item_ids()


async def test_failed_sync_falls_back_to_the_existing_index(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    ix.clock.advance(timedelta(minutes=6))

    class Revoked:
        def __getattr__(self, name: str) -> Any:
            async def fail(*a: object, **k: object) -> None:
                raise PermissionRevoked("합성", reason="invalid_grant")

            return fail

    outcome, content = await run_tool(ix, {"query": "견적서"}, mail_connector=Revoked())
    assert not outcome.is_error and content["results"]
    assert content["indexed_minutes_ago"] == 6


class _BrokenEmbedder(FakeEmbedder):
    async def embed(self, texts: Any) -> Any:
        raise EmbeddingError("transient", "합성 장애")


async def test_embedding_failure_falls_back_to_keywords_only(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    ix.mi.embedder = _BrokenEmbedder()
    _, content = await run_tool(ix, {"query": "견적서"})
    assert [r["subject"] for r in content["results"]] == ["견적서 납기 협의"]
    outcome, content = await run_tool(ix, {"query": "?!"})  # 키워드도 없다
    assert outcome.is_error and content == {"error": "index_unavailable"}


async def test_run_uses_indexed_search_with_envelope_and_keeps_ids_out_of_state(
    make_harness: Make,
    owner_engine: Engine,  # noqa: F811
) -> None:
    script = [
        {
            "tool_calls": [
                {"name": SEARCH_INDEXED_MAIL, "arguments": {"query": f"{BODY_MARKER} 담당자"}}
            ]
        },
        {"tool_calls": [{"name": "read_mail", "arguments": {"handle": "M1"}}]},
        {"text": "5분 전 기준으로 찾았습니다."},
    ]
    h = await make_harness(script)
    cid = create_connection(owner_engine, h.ws, h.deps.ids)
    h.deps.mail_index = MailIndexDeps(
        db=h.db, clock=h.clock, ids=h.deps.ids, embedder=FakeEmbedder()
    )
    await service.add_scope(
        h.deps.mail_index, h.ws, cid, label_id="INBOX", label_name="INBOX", period_days=365
    )
    await sync_connection(h.deps.mail_index, h.conns.mail[ACCOUNT], h.ws, cid)

    run_id = await h.start("SYNTH 표식이 들어 있는 메일을 찾아 줘")
    await h.work()

    assert await h.status(run_id) == "succeeded"
    second, third = h.llm.requests[1], h.llm.requests[2]
    tool_message = second.messages[-1]
    assert tool_message.role == "tool"
    assert (tool_message.content or "").startswith('<orbit_data source="search_indexed_mail"')
    assert '"handle": "M1"' in (tool_message.content or "")
    for request in (second, third):  # 모델에는 제공자 ID가 보이지 않는다
        assert all("msg-a-001" not in (m.content or "") for m in request.messages)
    assert BODY_MARKER in (third.messages[-1].content or "")  # read_mail 본문
    # 핸들 -> ID 매핑은 서버 쪽 run_message에만 있다.
    async with h.db.workspace_session(h.ws) as s:
        handles = (
            await s.execute(
                text(
                    "SELECT content->'handles'->'messages'->>'M1' FROM run_message"
                    " WHERE role = 'tool' AND content->>'name' = 'search_indexed_mail'"
                )
            )
        ).scalar_one()
    assert handles == "msg-a-001"
    # 질의문과 본문은 그래프 체크포인트·작업 인자에 들어가지 않는다.
    async with h.db.workspace_session(h.ws) as s:
        for table, column in (
            ("checkpoints", "checkpoint::text"),
            ("checkpoint_blobs", "encode(blob, 'escape')"),
            ("checkpoint_writes", "encode(blob, 'escape')"),
        ):
            leaked = (
                await s.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {column} LIKE '%SYNTH-BODY%'")  # noqa: S608
                )
            ).scalar_one()
            assert leaked == 0
        args = (
            (
                await s.execute(
                    text(
                        "SELECT args::text FROM procrastinate_jobs"
                        " WHERE args->>'workspace_id' = :ws"
                    ),
                    {"ws": str(h.ws)},
                )
            )
            .scalars()
            .all()
        )
    assert args and all("SYNTH" not in a and "담당자" not in a for a in args)


async def test_run_without_index_deps_returns_unavailable(make_harness: Make) -> None:
    script = [
        {"tool_calls": [{"name": SEARCH_INDEXED_MAIL, "arguments": {"query": "견적"}}]},
        {"text": "색인을 쓸 수 없습니다."},
    ]
    h = await make_harness(script)
    run_id = await h.start("견적 얘기한 메일")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert '{"error": "index_unavailable"}' in (h.llm.requests[1].messages[-1].content or "")


async def test_api_run_fingerprint_follows_embedding_model_id(
    app_url: str, async_db: Any, workspaces: Any
) -> None:
    import httpx
    from pydantic import SecretStr

    from integration.mailindex_support import NOW
    from orbit.api.app import create_app
    from orbit.core.clock import FixedClock
    from orbit.core.config import Settings
    from orbit.core.ids import Uuid7Generator

    fingerprints: list[str] = []
    for model in ("text-embedding-3-small", "text-embedding-3-large"):
        ws = workspaces.create()
        clock = FixedClock(NOW)
        settings = Settings(
            database_url=SecretStr(app_url),
            env="dev",
            dev_workspace_id=ws,
            embedding_model_id=model,
        )
        api = create_app(settings, clock=clock, ids=Uuid7Generator(clock))
        async with api.router.lifespan_context(api):
            transport = httpx.ASGITransport(app=api)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.post("/api/dev/login")).status_code == 200
                headers = {"X-CSRF-Token": client.cookies.get("orbit_csrf") or ""}
                resp = await client.post("/api/runs", json={"message": "안녕"}, headers=headers)
                assert resp.status_code == 201, resp.text
        async with async_db.workspace_session(ws) as s:
            fingerprints.append(
                (await s.execute(text("SELECT config_fingerprint FROM run"))).scalar_one()
            )
            await s.execute(
                text("DELETE FROM procrastinate_jobs WHERE args->>'workspace_id' = :ws"),
                {"ws": str(ws)},
            )
    assert fingerprints[0] != fingerprints[1]


async def test_index_pending_is_flagged_while_a_new_scope_is_unsynced(ix: IndexEnv) -> None:
    await add_inbox(ix)
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    _, done = await run_tool(ix, {"query": "견적서"})
    assert done["results"] and "index_pending" not in done
    await service.add_scope(
        ix.mi, ix.ws, ix.cid, label_id="SENT", label_name="SENT", period_days=30
    )
    _, waiting = await run_tool(ix, {"query": "견적서"})
    assert (
        waiting["results"] and waiting["index_pending"] is True
    )  # 기존 결과는 그대로, 일부만이라는 표시
    await sync_connection(ix.mi, ix.mail, ix.ws, ix.cid)
    _, after = await run_tool(ix, {"query": "견적서"})
    assert "index_pending" not in after
