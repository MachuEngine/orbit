"""Run·승인 API (ADR-0012 §4, ADR-0003 조건 2, ADR-0005 조건 1).

- (모든 경로는 `/api` 아래다. 인증은 쿠키 세션, `orbit.api.auth`)
- `POST /runs`: 대화(없으면 생성), run, 사용자 메시지, `execute_run` 작업 등록을
  한 트랜잭션으로 한다.
- `GET /runs/{id}`, `GET /runs/{id}/events`(SSE), `GET /approvals/{id}`.
- `POST /approvals/{id}/decision`: run 행을 먼저 잠근 뒤(run -> approval) 승인을 조건부 갱신하고,
  성공한 요청만 `resume_run`을 같은 트랜잭션에 등록한다. 이미 결정됐거나 만료면 409.
  만료 시각이 지난 승인에 결정이 오면 `expired`로 바꾸고 run을 이어서 진행시키려고 `resume_run`을
  등록한 뒤 409를 돌려준다(run이 승인 대기에 영원히 멈추지 않게 한다).
- 모든 조회는 `workspace_session`을 거치고, 다른 워크스페이스의 행은 404다.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import procrastinate
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text

from orbit.api.auth import SESSION_COOKIE, current_workspace
from orbit.auth.sessions import lookup_session
from orbit.core.clock import Clock
from orbit.core.config import Settings, get_llm_settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.llm.config_info import llm_config_info
from orbit.records.approvals import decide_approval, expire_approval, get_approval
from orbit.records.jobs import defer_job
from orbit.records.runs import (
    TERMINAL_RUN_STATUSES,
    append_message,
    create_run,
    get_run,
    list_events,
)
from orbit.runflow.fingerprint import PATTERN, config_fingerprint
from orbit.runflow.jobs import EXECUTE_RUN, RESUME_RUN
from orbit.runflow.repo import (
    get_tool_call,
    is_deadlock,
    latest_approval_for_run,
    lock_run,
    workspace_exists,
)
from orbit.runflow.summary import approval_summary
from orbit.runflow.target import public_target

router = APIRouter()
Workspace = Annotated[uuid.UUID, Depends(current_workspace)]
SSE_BATCH = 100


class CreateRunBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None


class DecisionBody(BaseModel):
    decision: Literal["approve", "reject"]


def _llm_info() -> dict[str, Any] | None:
    """Run 설정 지문에 넣을 LLM 설정. LLM 설정이 없는 환경(테스트 등)에서는 None이다."""
    try:
        return llm_config_info(get_llm_settings())
    except ValidationError:
        return None


def _db(request: Request) -> AsyncDatabase:
    db: AsyncDatabase = request.app.state.db
    return db


def _clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def _ids(request: Request) -> Uuid7Generator:
    ids: Uuid7Generator = request.app.state.ids
    return ids


def _jobs(request: Request) -> procrastinate.App:
    app: procrastinate.App = request.app.state.jobs
    return app


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what}을 찾을 수 없다")


def _job_args(ws: uuid.UUID, run_id: uuid.UUID) -> dict[str, str]:
    return {"run_id": str(run_id), "workspace_id": str(ws)}


# ------------------------------------------------------------------ 런


def _request_hash(body: CreateRunBody) -> str:
    """같은 멱등 키의 재전송인지 가리는 해시. 메시지와 대화 ID만 본다."""
    raw = json.dumps(
        {
            "message": body.message,
            "conversation_id": None if body.conversation_id is None else str(body.conversation_id),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_idempotency_key(raw: str | None) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Idempotency-Key는 UUID 형식이어야 한다"
        ) from None


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def post_run(
    body: CreateRunBody,
    request: Request,
    response: Response,
    ws: Workspace,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    db, clock, ids = _db(request), _clock(request), _ids(request)
    settings: Settings = request.app.state.settings
    ikey = _parse_idempotency_key(idempotency_key)
    rhash = _request_hash(body) if ikey is not None else None
    async with db.workspace_session(ws) as s:
        if not await workspace_exists(s, ws):
            raise _not_found("워크스페이스")
        if ikey is not None:
            # 같은 (워크스페이스, 키)의 동시 요청을 트랜잭션 끝까지 직렬화한다. 뒤 요청은 앞 요청이
            # 커밋한 뒤 run을 찾는다.
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"run-idem:{ws}:{ikey}"},
            )
            prior = (
                await s.execute(
                    text(
                        "SELECT id, conversation_id, status, request_hash FROM run"
                        " WHERE workspace_id = :ws AND idempotency_key = :k"
                    ),
                    {"ws": ws, "k": ikey},
                )
            ).first()
            if prior is not None:
                if prior[3] != rhash:
                    raise HTTPException(status.HTTP_409_CONFLICT, "idempotency_key_reused")
                # 재전송 응답의 status는 처음 응답 때 값이 아니라 지금 run의 상태다.
                response.status_code = status.HTTP_200_OK
                return {
                    "run_id": str(prior[0]),
                    "conversation_id": str(prior[1]),
                    "status": str(prior[2]),
                }
        conversation_id = body.conversation_id
        if conversation_id is None:
            conversation_id = ids.next()
            await s.execute(
                text(
                    "INSERT INTO conversation (id, workspace_id, created_at)"
                    " VALUES (:id, :ws, :now)"
                ),
                {"id": conversation_id, "ws": ws, "now": clock.now()},
            )
        else:
            found = (
                await s.execute(
                    text("SELECT 1 FROM conversation WHERE id = :id AND workspace_id = :ws"),
                    {"id": conversation_id, "ws": ws},
                )
            ).first()
            if found is None:
                raise _not_found("대화")
        run = await create_run(
            s,
            workspace_id=ws,
            conversation_id=conversation_id,
            pattern=PATTERN,
            config_fingerprint=config_fingerprint(settings.llm_model_id, _llm_info()),
            clock=clock,
            ids=ids,
            idempotency_key=ikey,
            request_hash=rhash,
        )
        await append_message(
            s,
            workspace_id=ws,
            run_id=run.id,
            role="user",
            content={"text": body.message},
            clock=clock,
        )
        await defer_job(s, _jobs(request), task_name=EXECUTE_RUN, args=_job_args(ws, run.id))
    return {"run_id": str(run.id), "conversation_id": str(conversation_id), "status": run.status}


@router.get("/runs/{run_id}")
async def read_run(run_id: uuid.UUID, request: Request, ws: Workspace) -> dict[str, Any]:
    async with _db(request).workspace_session(ws) as s:
        run = await get_run(s, workspace_id=ws, run_id=run_id)
        if run is None:
            raise _not_found("run")
        approval = await latest_approval_for_run(s, ws, run_id)
    return {
        "id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "status": run.status,
        "error_kind": run.error_kind,
        "pending_approval_id": (
            str(approval.id) if approval is not None and approval.status == "pending" else None
        ),
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def _sse(seq: int, type_: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"id: {seq}\nevent: {type_}\ndata: {data}\n\n".encode()


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: uuid.UUID,
    request: Request,
    ws: Workspace,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """`Last-Event-ID`(또는 쿼리 `after`) 이후 이벤트를 재생하고, 새 이벤트를 폴링으로 기다린다.

    `after`가 있으면 그것을 우선한다. run이 종료 상태가 되고 남은 이벤트를 모두 보냈으면 닫는다.
    """
    cursor = 0
    if after is not None:
        cursor = after
    elif last_event_id is not None:
        try:
            cursor = max(0, int(last_event_id))
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Last-Event-ID가 정수가 아니다"
            ) from None
    db = _db(request)
    settings: Settings = request.app.state.settings
    async with db.workspace_session(ws) as s:
        if await get_run(s, workspace_id=ws, run_id=run_id) is None:
            raise _not_found("run")

    session_token = request.cookies.get(SESSION_COOKIE) or ""
    clock = _clock(request)

    recheck = timedelta(seconds=settings.sse_session_recheck_seconds)

    async def generate() -> AsyncIterator[bytes]:
        position = cursor
        last_checked: datetime | None = None
        while True:
            # 첫 회차와, 주입된 시계 기준 마지막 확인 뒤 재확인 주기가 지났을 때 세션을 다시
            # 확인한다. 로그아웃·만료 뒤에는 스트림을 닫는다.
            now = clock.now()
            if last_checked is None or now - last_checked >= recheck:
                if await lookup_session(db, session_token, clock=clock) is None:
                    return
                last_checked = now
            # 상태를 먼저 읽고 이벤트를 읽는다. 종료 상태를 본 뒤 남은 이벤트를 모두 보내야 한다.
            async with db.workspace_session(ws) as s:
                run = await get_run(s, workspace_id=ws, run_id=run_id)
                if run is None:
                    return
                events = await list_events(
                    s, workspace_id=ws, run_id=run_id, after_seq=position, limit=SSE_BATCH
                )
            for event in events:
                yield _sse(event.seq, event.type, event.payload)
                position = event.seq
            if events:
                continue
            if run.status in TERMINAL_RUN_STATUSES:
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(settings.sse_poll_interval_seconds)

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


# ------------------------------------------------------------------ 승인


@router.get("/approvals/{approval_id}")
async def read_approval(approval_id: uuid.UUID, request: Request, ws: Workspace) -> dict[str, Any]:
    async with _db(request).workspace_session(ws) as s:
        approval = await get_approval(s, workspace_id=ws, approval_id=approval_id)
        if approval is None:
            raise _not_found("승인")
        tool_call = await get_tool_call(s, ws, approval.tool_call_id)
    if tool_call is None:
        raise _not_found("승인")
    handles = tool_call.provenance.get("handles", {})
    return {
        "id": str(approval.id),
        "run_id": str(approval.run_id),
        "status": approval.status,
        "tool_name": tool_call.tool_name,
        "args": tool_call.args,
        "summary": approval_summary(tool_call.tool_name, tool_call.args, handles),
        "provenance": tool_call.provenance.get("emails", {}),
        "target": public_target(tool_call.provenance.get("target")),
        "expires_at": approval.expires_at.isoformat(),
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
    }


@router.post("/approvals/{approval_id}/decision")
async def post_decision(
    approval_id: uuid.UUID, body: DecisionBody, request: Request, ws: Workspace
) -> dict[str, str]:
    outcome: str | None = None
    for attempt in range(2):  # 교착(40P01)은 한 번 다시 시도한다
        try:
            outcome = await _decide(request, ws, approval_id, approve=body.decision == "approve")
            break
        except Exception as exc:
            if attempt == 0 and is_deadlock(exc):
                continue
            raise
    if outcome == "not_found":
        raise _not_found("승인")
    if outcome == "decided":
        return {"status": "approved" if body.decision == "approve" else "rejected"}
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        "expired" if outcome == "expired" else "이미 결정됐거나 결정할 수 없는 상태다",
    )


async def _decide(request: Request, ws: uuid.UUID, approval_id: uuid.UUID, *, approve: bool) -> str:
    """`decided`, `expired`, `conflict`, `not_found` 중 하나를 돌려준다. 잠금은 run -> approval."""
    db, clock = _db(request), _clock(request)
    async with db.workspace_session(ws) as s:
        approval = await get_approval(s, workspace_id=ws, approval_id=approval_id)
        if approval is None:
            return "not_found"
        run_status = await lock_run(s, ws, approval.run_id)
        if run_status != "waiting_approval":
            return "conflict"
        decided = await decide_approval(
            s, workspace_id=ws, approval_id=approval_id, approve=approve, clock=clock
        )
        if decided:
            await defer_job(
                s, _jobs(request), task_name=RESUME_RUN, args=_job_args(ws, approval.run_id)
            )
            return "decided"
        if await expire_approval(s, workspace_id=ws, approval_id=approval_id, clock=clock):
            await defer_job(
                s, _jobs(request), task_name=RESUME_RUN, args=_job_args(ws, approval.run_id)
            )
            return "expired"
        return "conflict"
