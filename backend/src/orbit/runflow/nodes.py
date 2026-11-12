"""그래프 노드 (ADR-0003 조건 1~4, ADR-0006, ADR-0012 §4).

노드 구성
- `agent`: LLM 호출, 대화 저장, 읽기 도구 실행. 쓰기 도구 호출이면 `tool_call`과 `approval`을 만들고
  run을 `waiting_approval`로 옮긴다. 도구 호출이 없는 응답이면 run을 `succeeded`로 끝낸다.
- `await_approval`: `interrupt`만 한다. 부수 효과가 없다(조건 3).
- `execute`: Orbit의 `approval` 상태를 읽고 판단한다(조건 4). 승인이면 저장된 인자로 동작을 만들어
  `args_hash`를 재계산하고, 일치할 때만 `execute_approved`를 부른다. 거절·만료면 거절 결과만 남긴다.

step 멱등(장애 후 노드 재실행)
- step마다 assistant `run_message`는 하나다. 이미 있으면 LLM을 다시 부르지 않고 저장된 응답을 쓴다.
- `tool_call` id는 (run_id, step, 호출 순번)에서 UUIDv5로 만든다. 같은 호출은 한 번만 기록된다.
- 도구 결과·이벤트는 저장 전에 같은 step의 기록이 있는지 확인한다(run 행을 잠근 뒤 확인하므로
  동시에 재실행돼도 중복되지 않는다).

그래프 상태에는 ID와 step만 있다. 본문·주소는 `run_message`와 `tool_call`(RLS 테이블)에만 둔다.
로그에도 본문·주소를 남기지 않는다(예외 메시지에는 ID와 종류만).
"""

import json
import logging
import uuid
from typing import Annotated, Any

from langgraph.types import interrupt
from pydantic import Field, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.actions.executor import ApprovalRejected, ApprovalView, execute_approved
from orbit.actions.hashing import args_hash, canonical_args
from orbit.actions.models import WriteAction
from orbit.actions.provenance import classify_emails
from orbit.assistant.tools import CALENDAR_TOOLS, ReadOnlyTools, ToolInputError
from orbit.assistant.workset import WorkSet
from orbit.connectors.contracts import CalendarConnector, MailConnector
from orbit.connectors.errors import CONNECTOR_NOTICE_KINDS
from orbit.llm.interface import LLMError
from orbit.llm.types import LLMRequest, LLMResponse
from orbit.records.approvals import create_tool_call_with_approval
from orbit.records.executions import SqlExecutionStore
from orbit.records.runs import (
    TERMINAL_RUN_STATUSES,
    append_event,
    append_message,
    list_events,
    list_messages,
    transition_run,
)
from orbit.runflow.context import ConversationContext, load_context
from orbit.runflow.deps import NoConnection, RunDeps
from orbit.runflow.jobs import EXPIRE_APPROVALS, defer_at
from orbit.runflow.prompt import build_system_prompt
from orbit.runflow.repo import get_approval_by_tool_call, get_tool_call, lock_run
from orbit.runflow.state import RunState, validate_state
from orbit.runflow.store import ActionsStoreAdapter
from orbit.runflow.summary import approval_summary
from orbit.runflow.target import build_target
from orbit.runflow.transcript import Transcript
from orbit.runflow.write_tools import (
    READ_TOOLS,
    WRITE_TOOLS,
    build_action,
    recipient_emails,
    run_tool_specs,
)

TOOL_CALL_NAMESPACE = uuid.UUID("5f0c1b7e-8a3d-4c6e-9b21-0d4e7a6f1c33")
TEMPERATURE = 0.0
_ACTION_ADAPTER: TypeAdapter[WriteAction] = TypeAdapter(
    Annotated[WriteAction, Field(discriminator="kind")]
)


def orbit_tool_call_id(run_id: uuid.UUID, step: int, index: int) -> uuid.UUID:
    """(run, step, 호출 순번)에서 결정적으로 만든 `tool_call` id. 노드를 재실행해도 같은 값이다."""
    return uuid.uuid5(TOOL_CALL_NAMESPACE, f"{run_id}:{step}:{index}")


logger = logging.getLogger(__name__)


class ExecutionNotSettled(RuntimeError):
    """실행 기록이 아직 끝나지 않았다(다른 실행자가 소유 중이거나 대조 미완료). 재시도한다."""


class ApprovalStillPending(RuntimeError):
    """승인이 아직 결정되지 않았는데 execute에 도달했다(재개 조건 위반)."""


def _text(content: dict[str, Any]) -> str:
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


class RunNodes:
    """한 번의 그래프 실행에 쓰는 노드 묶음. 의존성은 생성자로 주입한다."""

    def __init__(self, deps: RunDeps) -> None:
        self._d = deps

    # ------------------------------------------------------------ 공통

    @staticmethod
    def _ids(state: RunState) -> tuple[uuid.UUID, uuid.UUID, int]:
        valid = validate_state(state)
        return (
            uuid.UUID(valid["workspace_id"]),
            uuid.UUID(valid["run_id"]),
            int(valid.get("step", 0)),
        )

    @staticmethod
    def _out(update: dict[str, Any]) -> dict[str, Any]:
        return validate_state(update)

    async def _transition(
        self,
        s: AsyncSession,
        ws: uuid.UUID,
        run_id: uuid.UUID,
        to_status: str,
        error_kind: str | None = None,
    ) -> None:
        await transition_run(
            s,
            workspace_id=ws,
            run_id=run_id,
            to_status=to_status,
            clock=self._d.clock,
            error_kind=error_kind,
        )
        if error_kind is not None:
            await append_event(
                s,
                workspace_id=ws,
                run_id=run_id,
                type="error",
                payload={"error_kind": error_kind},
                clock=self._d.clock,
            )
        await append_event(
            s,
            workspace_id=ws,
            run_id=run_id,
            type="status",
            payload={"status": to_status},
            clock=self._d.clock,
        )

    async def _emit_once(
        self,
        s: AsyncSession,
        ws: uuid.UUID,
        run_id: uuid.UUID,
        type_: str,
        step: int,
        payload: dict[str, Any],
    ) -> None:
        """같은 (type, step) 이벤트가 이미 있으면 건너뛴다. 호출자가 run을 잠근 상태여야 한다."""
        existing = await list_events(s, workspace_id=ws, run_id=run_id)
        if any(e.type == type_ and e.payload.get("step") == step for e in existing):
            return
        await append_event(
            s,
            workspace_id=ws,
            run_id=run_id,
            type=type_,
            payload={"step": step, **payload},
            clock=self._d.clock,
        )

    async def _notice_connector_error(
        self,
        ws: uuid.UUID,
        run_id: uuid.UUID,
        step: int,
        tool_name: str,
        kind: str,
        reason: str,
    ) -> None:
        """커넥터 오류 원인을 알리는 이벤트를 남긴다. 같은 (step, service, kind)는 한 번만."""
        if kind not in CONNECTOR_NOTICE_KINDS:
            return
        service = "calendar" if tool_name in CALENDAR_TOOLS else "mail"
        async with self._d.db.workspace_session(ws) as s:
            if await lock_run(s, ws, run_id) is None:
                return
            existing = await list_events(s, workspace_id=ws, run_id=run_id)
            if any(
                e.type == "connector_notice"
                and e.payload.get("step") == step
                and e.payload.get("service") == service
                and e.payload.get("kind") == kind
                for e in existing
            ):
                return
            await append_event(
                s,
                workspace_id=ws,
                run_id=run_id,
                type="connector_notice",
                payload={"step": step, "service": service, "kind": kind, "reason": reason},
                clock=self._d.clock,
            )
        logger.warning(
            "커넥터 오류: run_id=%s service=%s kind=%s reason=%s", run_id, service, kind, reason
        )

    async def _prior(self, ws: uuid.UUID, run_id: uuid.UUID) -> ConversationContext:
        """같은 대화의 앞선 종료 run에서 맥락을 다시 만든다(ADR-0017)."""
        async with self._d.db.workspace_session(ws) as s:
            return await load_context(s, ws, run_id)

    async def _transcript(
        self, ws: uuid.UUID, run_id: uuid.UUID, prior: ConversationContext | None = None
    ) -> Transcript:
        async with self._d.db.workspace_session(ws) as s:
            rows = await list_messages(s, workspace_id=ws, run_id=run_id)
        return Transcript(tuple(rows), prior) if prior is not None else Transcript(tuple(rows))

    async def _fail(self, ws: uuid.UUID, run_id: uuid.UUID, kind: str) -> dict[str, Any]:
        async with self._d.db.workspace_session(ws) as s:
            status = await lock_run(s, ws, run_id)
            if status is not None and status not in TERMINAL_RUN_STATUSES:
                await self._transition(s, ws, run_id, "failed", kind)
                logger.warning("run 실패: run_id=%s error_kind=%s", run_id, kind)
        return self._out({"next_node": "done"})

    async def _store_tool_result(
        self,
        ws: uuid.UUID,
        run_id: uuid.UUID,
        step: int,
        llm_call_id: str,
        name: str,
        result: dict[str, Any] | str,
        *,
        is_error: bool,
        handles: dict[str, Any] | None = None,
        orbit_tool_call_id_: uuid.UUID | None = None,
    ) -> None:
        """도구 결과 행을 저장한다. 같은 (step, 호출 ID)가 이미 있으면 건너뛴다."""
        async with self._d.db.workspace_session(ws) as s:
            if await lock_run(s, ws, run_id) is None:
                return
            rows = await list_messages(s, workspace_id=ws, run_id=run_id)
            if llm_call_id in Transcript(tuple(rows)).tool_results(step):
                return
            # `response.provider_items`(추론 항목 등)는 그래프 상태·DB에 저장하지 않는다(결정:
            # 프롬프트 파생 내용을 저장소에 늘리지 않는다). 그래서 워커 경로는 이 항목을 버리고,
            # 다음 단계 요청에도 되돌려 보내지 않는다(docs/pilot/README.md 한계).
            content: dict[str, Any] = {
                "step": step,
                "tool_call_id": llm_call_id,
                "name": name,
                "text": result if isinstance(result, str) else _text(result),
                "is_error": is_error,
            }
            if handles is not None:
                content["handles"] = handles
            if orbit_tool_call_id_ is not None:
                content["orbit_tool_call_id"] = str(orbit_tool_call_id_)
            await append_message(
                s,
                workspace_id=ws,
                run_id=run_id,
                role="tool",
                content=content,
                clock=self._d.clock,
            )

    # ------------------------------------------------------------ agent

    async def agent(self, state: RunState) -> dict[str, Any]:
        d = self._d
        ws, run_id, step = self._ids(state)

        async with d.db.workspace_session(ws) as s:
            status = await lock_run(s, ws, run_id)
            if status is None or status in TERMINAL_RUN_STATUSES:
                return self._out({"next_node": "done"})
            if status == "queued":
                await self._transition(s, ws, run_id, "running")
        if step >= d.max_steps:
            return await self._fail(ws, run_id, "max_steps")

        prior = await self._prior(ws, run_id)
        transcript = await self._transcript(ws, run_id, prior)
        assistant = transcript.assistant_for_step(step)
        if assistant is None:
            request = LLMRequest(
                messages=tuple(transcript.llm_messages(build_system_prompt(d.clock.now(), d.tz))),
                purpose="system_under_test",
                tools=run_tool_specs(),
                tool_choice="auto",
                temperature=TEMPERATURE,
            )
            try:
                response = await d.llm.complete(request)
            except LLMError as exc:
                return await self._fail(ws, run_id, type(exc).__name__)
            assistant = await self._store_assistant(ws, run_id, step, response)
            transcript = await self._transcript(ws, run_id, prior)

        calls: list[dict[str, Any]] = list(assistant.get("tool_calls", []))
        text = assistant.get("text")
        if isinstance(text, str) and text.strip():
            async with d.db.workspace_session(ws) as s:
                if await lock_run(s, ws, run_id) is not None:
                    await self._emit_once(s, ws, run_id, "assistant_text", step, {"text": text})
        if not calls:
            async with d.db.workspace_session(ws) as s:
                if await lock_run(s, ws, run_id) == "running":
                    await self._transition(s, ws, run_id, "succeeded")
            return self._out({"next_node": "done"})

        try:
            calendar, mail = await d.resolver.connectors_for(ws)
        except NoConnection:
            return await self._fail(ws, run_id, "no_connection")
        workset = transcript.workset()
        pending = await self._run_calls(
            ws, run_id, step, calls, transcript, workset, calendar, mail
        )
        if pending is None:
            return self._out({"step": step + 1, "tool_call_id": "", "next_node": "agent"})
        async with d.db.workspace_session(ws) as s:
            approval = await get_approval_by_tool_call(s, ws, pending)
        # 결정이 이미 났다면(재실행 중 승인·거절이 들어온 경우) interrupt 없이 바로 판단한다.
        route = "await_approval" if approval is None or approval.status == "pending" else "execute"
        return self._out({"step": step, "tool_call_id": str(pending), "next_node": route})

    async def _store_assistant(
        self, ws: uuid.UUID, run_id: uuid.UUID, step: int, response: LLMResponse
    ) -> dict[str, Any]:
        """step의 assistant 행을 저장한다. 이미 있으면(동시 재실행) 저장된 것을 돌려준다."""
        async with self._d.db.workspace_session(ws) as s:
            await lock_run(s, ws, run_id)
            rows = await list_messages(s, workspace_id=ws, run_id=run_id)
            existing = Transcript(tuple(rows)).assistant_for_step(step)
            if existing is not None:
                return existing
            content: dict[str, Any] = {
                "step": step,
                "text": response.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in response.tool_calls
                ],
            }
            await append_message(
                s,
                workspace_id=ws,
                run_id=run_id,
                role="assistant",
                content=content,
                clock=self._d.clock,
            )
            return content

    async def _run_calls(
        self,
        ws: uuid.UUID,
        run_id: uuid.UUID,
        step: int,
        calls: list[dict[str, Any]],
        transcript: Transcript,
        workset: WorkSet,
        calendar: CalendarConnector,
        mail: MailConnector,
    ) -> uuid.UUID | None:
        """도구 호출을 처리한다. 승인 대기가 생기면 그 `tool_call` id를 돌려준다."""
        done = transcript.tool_results(step)
        tools = ReadOnlyTools(calendar=calendar, mail=mail, tz=self._d.tz, workset=workset)
        first_write = next((i for i, c in enumerate(calls) if c["name"] in WRITE_TOOLS), None)
        for index, call in enumerate(calls):
            llm_id, name = str(call["id"]), str(call["name"])
            if name in WRITE_TOOLS:
                if index != first_write and llm_id not in done:
                    await self._store_tool_result(
                        ws,
                        run_id,
                        step,
                        llm_id,
                        name,
                        {"error": "one_write_at_a_time"},
                        is_error=True,
                    )
                continue
            if llm_id in done:
                continue
            if name not in READ_TOOLS:
                await self._store_tool_result(
                    ws, run_id, step, llm_id, name, {"error": "unknown_tool"}, is_error=True
                )
                continue
            outcome = await tools.execute(name, call.get("arguments"))  # type: ignore[arg-type]
            if outcome.connector_error is not None:
                # 결과 저장보다 먼저 남긴다. 저장 직후 중단돼도 재실행이 알림을 다시 만들지 않으면서
                # 빠뜨리지도 않는다.
                kind, reason = outcome.connector_error
                await self._notice_connector_error(ws, run_id, step, name, kind, reason)
            await self._store_tool_result(
                ws,
                run_id,
                step,
                llm_id,
                name,
                outcome.content,
                is_error=outcome.is_error,
                handles=tools.workset.export(),
            )
        if first_write is None:
            return None
        call = calls[first_write]
        transcript = await self._transcript(
            ws, run_id, transcript.prior
        )  # 이번 step의 조회 결과를 출처 분류에 포함한다
        return await self._request_approval(
            ws, run_id, step, first_write, call, transcript, tools.workset, calendar, mail
        )

    async def _request_approval(
        self,
        ws: uuid.UUID,
        run_id: uuid.UUID,
        step: int,
        index: int,
        call: dict[str, Any],
        transcript: Transcript,
        workset: WorkSet,
        calendar: CalendarConnector,
        mail: MailConnector,
    ) -> uuid.UUID | None:
        d = self._d
        llm_id, name = str(call["id"]), str(call["name"])
        tool_call_id = orbit_tool_call_id(run_id, step, index)
        if llm_id in transcript.tool_results(step):
            return None  # 이미 오류 결과를 남긴 호출이다
        async with d.db.workspace_session(ws) as s:
            existing = await get_tool_call(s, ws, tool_call_id)
        if existing is not None:
            return tool_call_id  # 이미 승인 요청을 만들었다(재실행)

        try:
            action = await build_action(
                name, call.get("arguments"), workset=workset, calendar=calendar, mail=mail
            )
        except ToolInputError as exc:
            await self._store_tool_result(
                ws, run_id, step, llm_id, name, {"error": exc.kind}, is_error=True
            )
            return None
        args = canonical_args(action)
        workspace_emails, external_texts = transcript.provenance_inputs()
        handles = _handles_of(name, call.get("arguments"))
        provenance: dict[str, Any] = {
            "emails": classify_emails(
                recipient_emails(action),
                user_utterances=transcript.user_utterances(),
                workspace_emails=workspace_emails,
                external_texts=external_texts,
            ),
            "handles": handles,
            "target": await build_target(action, calendar=calendar, mail=mail),
        }
        async with d.db.workspace_session(ws) as s:
            status = await lock_run(s, ws, run_id)
            if status is None or status in TERMINAL_RUN_STATUSES:
                return None
            _, approval, created = await create_tool_call_with_approval(
                s,
                workspace_id=ws,
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool_name=name,
                args=args,
                args_hash=args_hash(action),
                provenance=provenance,
                clock=d.clock,
                ids=d.ids,
                ttl=d.approval_ttl,
            )
            if created and d.jobs is not None:
                # 만료 시각에 확인 작업을 예약한다. 승인 행과 같은 트랜잭션이고, run 잠금 뒤라
                # 잠금 순서(run -> approval)를 지킨다. 워크스페이스 밖에서 만료분을 훑을 수 없어
                # (RLS) 주기 훑기 대신 승인마다 예약한다.
                await defer_at(
                    s,
                    d.jobs,
                    task_name=EXPIRE_APPROVALS,
                    workspace_id=ws,
                    run_id=run_id,
                    at=approval.expires_at,
                )
            if created:
                await append_event(
                    s,
                    workspace_id=ws,
                    run_id=run_id,
                    type="approval_requested",
                    payload={
                        "step": step,
                        "approval_id": str(approval.id),
                        "tool_name": name,
                        "summary": approval_summary(name, args, handles),
                    },
                    clock=d.clock,
                )
            if status == "running":
                await self._transition(s, ws, run_id, "waiting_approval")
        return tool_call_id

    # ------------------------------------------------------------ await_approval

    async def await_approval(self, state: RunState) -> dict[str, Any]:
        """interrupt만 한다. 이 앞뒤에 부수 효과를 두지 않는다(ADR-0003 조건 3)."""
        valid = validate_state(state)
        interrupt({"tool_call_id": valid.get("tool_call_id", "")})
        return self._out({"next_node": "execute"})

    # ------------------------------------------------------------ execute

    async def execute(self, state: RunState) -> dict[str, Any]:
        d = self._d
        ws, run_id, step = self._ids(state)
        tool_call_id = uuid.UUID(validate_state(state)["tool_call_id"])
        again = self._out({"step": step + 1, "tool_call_id": "", "next_node": "agent"})

        async with d.db.workspace_session(ws) as s:
            status = await lock_run(s, ws, run_id)
            if status is None or status in TERMINAL_RUN_STATUSES:
                return self._out({"next_node": "done"})
            tool_call = await get_tool_call(s, ws, tool_call_id)
            approval = await get_approval_by_tool_call(s, ws, tool_call_id)
            rows = await list_messages(s, workspace_id=ws, run_id=run_id)
            if tool_call is None or approval is None:
                pass  # 아래에서 실패로 끝낸다
            elif tool_call.run_id != run_id or approval.run_id != run_id:
                pass  # 다른 run의 기록이다. 아래에서 외부 호출 없이 실패로 끝낸다
            elif Transcript(tuple(rows)).tool_result_for_orbit_call(str(tool_call_id)) is not None:
                return again  # 이미 처리했다(재실행)
            elif approval.status == "pending":
                raise ApprovalStillPending(f"approval {approval.id}이 아직 결정되지 않았다")
            elif status == "waiting_approval":
                await self._transition(s, ws, run_id, "running")
        if tool_call is None or approval is None:
            return await self._fail(ws, run_id, "approval_missing")
        if tool_call.run_id != run_id or approval.run_id != run_id:
            return await self._fail(ws, run_id, "run_mismatch")

        llm_id = _llm_call_id(Transcript(tuple(rows)), step, tool_call_id, run_id)
        if approval.status in ("rejected", "expired"):
            await self._finish_call(
                ws, run_id, step, llm_id, tool_call, approval.id, approval.status, None
            )
            return again

        # approved: 저장된 인자로 동작을 다시 만들고 해시를 재계산한다(같을 때만 실행).
        try:
            action = _ACTION_ADAPTER.validate_python(tool_call.args)
        except ValueError:
            return await self._fail(ws, run_id, "hash_mismatch")
        digest = args_hash(action)
        if digest != approval.args_hash or digest != tool_call.args_hash:
            await self._finish_call(
                ws, run_id, step, llm_id, tool_call, approval.id, "failed", "hash_mismatch"
            )
            return await self._fail(ws, run_id, "hash_mismatch")

        try:
            calendar, mail = await d.resolver.connectors_for(ws)
        except NoConnection:
            return await self._fail(ws, run_id, "no_connection")
        store = ActionsStoreAdapter(SqlExecutionStore(d.db, ws, clock=d.clock, ids=d.ids))
        view = ApprovalView(
            id=approval.id,
            status=approval.status,
            args_hash=approval.args_hash,
            expires_at=approval.expires_at,
        )
        try:
            record = await execute_approved(
                view,
                action,
                calendar=calendar,
                mail=mail,
                store=store,
                clock=d.clock,
                lease_seconds=d.lease_seconds,
            )
        except ApprovalRejected as exc:
            if exc.reason == "hash_mismatch":
                await self._finish_call(
                    ws, run_id, step, llm_id, tool_call, approval.id, "failed", "hash_mismatch"
                )
                return await self._fail(ws, run_id, "hash_mismatch")
            await self._finish_call(
                ws, run_id, step, llm_id, tool_call, approval.id, "expired", None
            )
            return again
        if record.status in ("pending", "unknown"):
            # 다른 실행자가 소유 중이거나 대조를 끝내지 못했다. 재호출하지 않고 작업을 재시도한다.
            raise ExecutionNotSettled(f"execution {record.id} 상태: {record.status}")
        await self._finish_call(
            ws, run_id, step, llm_id, tool_call, approval.id, record.status, record.error_kind
        )
        return again

    async def _finish_call(
        self,
        ws: uuid.UUID,
        run_id: uuid.UUID,
        step: int,
        llm_id: str,
        tool_call: Any,
        approval_id: uuid.UUID,
        status: str,
        error_kind: str | None,
    ) -> None:
        """쓰기 호출의 도구 결과와 `execution_result` 이벤트를 남긴다(둘 다 멱등)."""
        result: dict[str, Any] = {"status": status}
        if error_kind is not None:
            result["error_kind"] = error_kind
        await self._store_tool_result(
            ws,
            run_id,
            step,
            llm_id,
            tool_call.tool_name,
            result,
            is_error=status not in ("succeeded",),
            orbit_tool_call_id_=tool_call.id,
        )
        async with self._d.db.workspace_session(ws) as s:
            if await lock_run(s, ws, run_id) is not None:
                payload: dict[str, Any] = {"approval_id": str(approval_id), "status": status}
                if error_kind is not None:
                    payload["error_kind"] = error_kind
                await self._emit_once(s, ws, run_id, "execution_result", step, payload)


def _handles_of(name: str, arguments: Any) -> dict[str, str]:
    """승인 화면 요약에 쓸 핸들. 제공자 ID는 담지 않는다."""
    if not isinstance(arguments, dict):
        return {}
    handles: dict[str, str] = {}
    if name in ("update_event_time", "cancel_event") and isinstance(arguments.get("handle"), str):
        handles["event"] = arguments["handle"].strip().upper()[:16]
    if name == "send_mail" and isinstance(arguments.get("reply_to_handle"), str):
        handles["reply_to"] = arguments["reply_to_handle"].strip().upper()[:16]
    return handles


def _llm_call_id(
    transcript: Transcript, step: int, tool_call_id: uuid.UUID, run_id: uuid.UUID
) -> str:
    """Orbit `tool_call` id에 대응하는 LLM 호출 ID를 assistant 행에서 찾는다."""
    assistant = transcript.assistant_for_step(step) or {}
    for index, call in enumerate(assistant.get("tool_calls", [])):
        if orbit_tool_call_id(run_id, step, index) == tool_call_id:
            return str(call["id"])
    raise LookupError("tool_call에 대응하는 호출을 찾을 수 없다")
