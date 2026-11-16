"""정답 제출 오라클: 과제의 정답을 그대로 내는 스크립트 LLM 응답과 호출 목록을 만든다.

테스트(`tests/eval_tasks_support.py`)와 러너가 함께 쓴다. 러너는 이 오라클이 쓰는 도구 호출 수를
"과제별 최소 호출 수"로 기록한다(ADR-0029 §2, `min_tool_calls`). 도구 호출로 일정·메일·문서의
핸들을 만든 뒤(미리 같은 도구를 실행해 핸들을 알아낸다) 정답 핸들로 제출한다. 오라클은 정답을
이미 알고 있어 답을 알기 위한 탐색(예: 요청 메일 읽기)은 호출 수에 들지 않는다. 그래서 최소 호출
수는 실제로 필요한 호출 수보다 작거나 같은 하한이다.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from orbit.assistant.tools import ReadOnlyTools
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.evaldata.schema import (
    AbstainAnswer,
    ClarifyAnswer,
    DocumentAnswer,
    DraftSentChange,
    EventAnswer,
    EventCancelledChange,
    EventCreatedChange,
    EventSetAnswer,
    EventTimeAnswer,
    EventUpdatedChange,
    MessageSetAnswer,
    ReminderCreatedChange,
    Task,
    TimeSlotAnswer,
)
from orbit.runflow.docs_tool import DocTools


def iso(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).isoformat()


def day_window(tz: ZoneInfo, lo: datetime, hi: datetime) -> dict[str, str]:
    """`lo`의 현지 날짜 0시부터 `hi`의 현지 날짜 다음 날 0시까지."""
    first = lo.astimezone(tz).date()
    last = (hi.astimezone(tz) - timedelta(microseconds=1)).date()
    start = datetime.combine(first, time.min, tzinfo=tz)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz)
    return {"time_min": start.isoformat(), "time_max": end.isoformat()}


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


class OracleError(AssertionError):
    pass


class Oracle:
    """한 과제의 정답 제출 스크립트를 만든다."""

    def __init__(self, world: SimWorld, account: Mapping[str, Any], task: Task) -> None:
        """`world`는 과제 시각에 맞춘 포크, `account`는 세계 스냅샷의 과제 계정 항목이다."""
        self.task = task
        self.tz = ZoneInfo(task.time_zone or "Asia/Seoul")
        conn = build_sim_connectors(world, [task.account_id])
        acct = task.account_id
        self.tools = ReadOnlyTools(calendar=conn.calendar[acct], mail=conn.mail[acct], tz=self.tz)
        self.docs = DocTools(conn.drive[acct], self.tools.workset, self.tz)
        self.account = account
        self._event: dict[tuple[str, str], Any] = {
            (e["calendar_id"], e["id"]): e for e in self.account["events"]
        }
        self._message = {m["id"]: m for m in self.account["messages"]}
        self._file = {f["id"]: f for f in self.account["files"]}

    # ------------------------------------------------------------ 핸들 노출

    async def expose_events(
        self, refs: Sequence[tuple[str, str]]
    ) -> tuple[dict[str, Any], dict[tuple[str, str], str]]:
        """기대 일정을 모두 담는 `list_events` 호출과 {(calendar, id): 핸들}을 돌려준다."""
        spans = []
        for key in refs:
            e = self._event[key]
            spans.append((datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])))
        window = day_window(self.tz, min(s for s, _ in spans), max(e for _, e in spans))
        out = await self.tools.execute("list_events", dict(window))
        if out.is_error:
            raise OracleError(f"list_events 실패: {out.content}")
        handles = {key: self.tools.workset.event_handle(*key) for key in refs}
        listed = {m for m in self._listed_ids(out.content)}
        if not {h for h in handles.values()} <= listed:
            raise OracleError("정답 일정이 조회 결과에 없다")
        return call("list_events", **window), handles

    @staticmethod
    def _listed_ids(content: str) -> set[str]:
        data = json.loads(content)
        return {e["handle"] for e in data.get("events", [])}

    async def expose_message(self, message_id: str) -> tuple[dict[str, Any], str]:
        subject = self._message[message_id]["subject"]
        query = f'subject:"{subject}"'
        out = await self.tools.execute("search_mail", {"query": query, "max_results": 20})
        if out.is_error:
            raise OracleError(f"search_mail 실패: {out.content}")
        handle = self.tools.workset.message_handle(message_id)
        if handle not in {m["handle"] for m in json.loads(out.content)["messages"]}:
            raise OracleError("정답 메일이 검색 결과에 없다")
        return call("search_mail", query=query, max_results=20), handle

    async def expose_document(self, file_id: str) -> tuple[dict[str, Any], str]:
        name = self._file[file_id]["name"]
        out = await self.docs.execute("search_docs", {"text": name})
        if out.is_error:
            raise OracleError(f"search_docs 실패: {out.content}")
        handle = self.tools.workset.document_handle(file_id)
        if handle not in {f["handle"] for f in json.loads(out.content)["files"]}:
            raise OracleError("정답 문서가 검색 결과에 없다")
        return call("search_docs", text=name), handle

    # ------------------------------------------------------------ 정답 → 단계

    def text(self) -> str:
        facts = [f.any_of[0] for f in self.task.gold.answer_facts]
        return "정답: " + ", ".join(facts) if facts else "확인했습니다."

    async def answer(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """(제출 전에 부를 조회 호출들, 구조화된 답). 구조화된 답이 없으면 None."""
        ans = self.task.gold.expected_answer
        tz = self.tz
        if isinstance(ans, EventSetAnswer):
            keys = [(e.calendar_id, e.event_id) for e in ans.events]
            c, h = await self.expose_events(keys)
            return [c], {"kind": "event_set", "events": [h[k] for k in keys]}
        if isinstance(ans, EventAnswer):
            key = (ans.event.calendar_id, ans.event.event_id)
            c, h = await self.expose_events([key])
            return [c], {"kind": "event", "event": h[key]}
        if isinstance(ans, EventTimeAnswer):
            key = (ans.event.calendar_id, ans.event.event_id)
            c, h = await self.expose_events([key])
            return [c], {"kind": "event_time", "event": h[key], "start": iso(ans.start, tz)}
        if isinstance(ans, TimeSlotAnswer):
            window = day_window(tz, ans.start, ans.start + timedelta(minutes=1))
            await self.tools.execute("list_events", dict(window))
            return [call("list_events", **window)], {
                "kind": "time_slot",
                "start": iso(ans.start, tz),
            }
        if isinstance(ans, MessageSetAnswer):
            ids = [m.message_id for m in ans.messages]
            calls, handles = [], []
            for mid in ids:
                c, mail_handle = await self.expose_message(mid)
                calls.append(c)
                handles.append(mail_handle)
            return calls, {"kind": "message_set", "messages": handles}
        if isinstance(ans, DocumentAnswer):
            c, doc_handle = await self.expose_document(ans.document.file_id)
            return [c], {"kind": "document", "document": doc_handle}
        if isinstance(ans, AbstainAnswer):
            return [], {"kind": "abstain"}
        if isinstance(ans, ClarifyAnswer):
            return [], {"kind": "clarify"}
        return [], None

    async def write(self, change: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """(쓰기 전에 부를 조회 호출들, 쓰기 도구 호출)."""
        tz = self.tz
        if isinstance(change, EventCreatedChange):
            args: dict[str, Any] = {
                "title": change.title_any_of[0],
                "start": iso(change.start, tz),
                "end": iso(change.end or change.start + timedelta(hours=1), tz),
                "attendees": list(change.attendees_include),
            }
            if change.location_any_of:
                args["location"] = change.location_any_of[0]
            return [], call("create_event", **args)
        if isinstance(change, EventUpdatedChange):
            key = (change.calendar_id, change.event_id)
            c, h = await self.expose_events([key])
            f = change.fields
            if f.start is None or f.end is None:
                raise OracleError("시각 변경 기대에 start·end가 필요하다")
            return [c], call(
                "update_event_time", handle=h[key], start=iso(f.start, tz), end=iso(f.end, tz)
            )
        if isinstance(change, EventCancelledChange):
            key = (change.calendar_id, change.event_id)
            c, h = await self.expose_events([key])
            return [c], call("cancel_event", handle=h[key])
        if isinstance(change, DraftSentChange):
            if change.reply_to_message_id is None:
                raise OracleError("회신 기대에 reply_to_message_id가 필요하다")
            c, handle = await self.expose_message(change.reply_to_message_id)
            body = " ".join(f.any_of[0] for f in change.body_facts) or "확인 후 회신드리겠습니다"
            return [c], call(
                "send_mail",
                to=list(change.to_include),
                subject="Re: 합성 회신",
                body=body + " 확인했습니다.",
                reply_to_handle=handle,
            )
        if isinstance(change, ReminderCreatedChange):
            text = change.text_any_of[0] if change.text_any_of else "마감 알림"
            return [], call("create_reminder", text=text, due_at=iso(change.due_at, tz))
        raise OracleError(f"지원하지 않는 기대 변화: {type(change).__name__}")


async def oracle_calls(
    world: SimWorld, account: Mapping[str, Any], task: Task
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(읽기 호출들, 쓰기 전 조회 호출들, 쓰기 호출들). 답이 있는 과제는 읽기, 아니면 쓰기 전
    조회를 쓴다."""
    o = Oracle(world, account, task)
    reads: list[dict[str, Any]] = []
    if task.gold.expected_answer is not None:
        reads, _ = await o.answer()
    pre: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    for change in task.gold.expected_changes or []:
        c, w = await o.write(change)
        pre += c
        writes.append(w)
    return reads, pre, writes


async def min_tool_calls(world: SimWorld, account: Mapping[str, Any], task: Task) -> int:
    """정답 제출 오라클이 쓰는 도구 호출 수(답 제출 제외). 결함 재시도는 세지 않는다."""
    reads, pre, writes = await oracle_calls(world, account, task)
    return len(reads or pre) + len(writes)
