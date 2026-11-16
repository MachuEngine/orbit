"""종단 과제 v1(`orbit.evaldata.v1.tasks`) 테스트 공용 도구. 데이터는 모두 합성 값이다.

- `real_task_world()`·`real_suite()`: 커밋된 세계 v1로 한 번 만든 설계도·과제(표현 변형 없이).
- `slim_dataset()`: 러너가 읽는 `Dataset`. 검색용 계정(메일 6,200통)은 빼서 에피소드 준비를 줄인다.
- `oracle_script()`: 과제의 정답을 그대로 제출하는 스크립트 LLM 응답. 도구 호출로 일정·메일·문서의
  핸들을 만든 뒤(미리 같은 도구를 실행해 핸들을 알아낸다) 정답 핸들로 제출한다. 정답 계산과 채점이
  맞물리는지 확인하는 용도다.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from orbit.assistant.tools import ReadOnlyTools
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
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
from orbit.evaldata.v1.task_gold import Ctx
from orbit.evaldata.v1.task_world import TaskWorld, load_task_world
from orbit.evaldata.v1.tasks import DEFAULT_PLAN, Candidates, Suite, build_candidates, build_suite
from orbit.evalrunner.task_source import Dataset
from orbit.runflow.docs_tool import DocTools

COMMITTED_V1 = Path(__file__).resolve().parents[2] / "eval" / "datasets" / "orbit-eval" / "v1"
SEED = 20261116


@functools.lru_cache(maxsize=1)
def real_task_world() -> tuple[TaskWorld, dict[str, Any]]:
    return load_task_world(COMMITTED_V1)


@functools.lru_cache(maxsize=1)
def real_candidates() -> Candidates:
    tw, _ = real_task_world()
    return build_candidates(Ctx(tw, SEED))


@functools.lru_cache(maxsize=1)
def real_suite() -> Suite:
    tw, _ = real_task_world()
    suite, _ = build_suite(
        tw, SEED, DEFAULT_PLAN, include_paraphrase=False, candidates=real_candidates()
    )
    return suite


def slim_dataset(tasks: Sequence[Task], world: dict[str, Any]) -> Dataset:
    """e2e 계정만 남긴 세계로 러너용 `Dataset`을 만든다."""
    accounts = {k: v for k, v in world["accounts"].items() if not k.startswith("acct-s")}
    snapshot = {**world, "accounts": accounts}
    return Dataset("orbit-eval", "v1", {}, snapshot, tuple(tasks))


_WORLDS: dict[int, SimWorld] = {}


def _world_for(snapshot: Any, as_of: datetime) -> SimWorld:
    key = id(snapshot)
    if key not in _WORLDS:
        _WORLDS[key] = SimWorld.load(dict(snapshot), FixedClock(as_of))
    return _WORLDS[key].fork(0, FixedClock(as_of))


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

    def __init__(self, snapshot: Any, task: Task) -> None:
        self.task = task
        self.tz = ZoneInfo(task.time_zone or "Asia/Seoul")
        world = _world_for(snapshot, task.as_of_time)
        conn = build_sim_connectors(world, [task.account_id])
        acct = task.account_id
        self.tools = ReadOnlyTools(calendar=conn.calendar[acct], mail=conn.mail[acct], tz=self.tz)
        self.docs = DocTools(conn.drive[acct], self.tools.workset, self.tz)
        self.account = snapshot["accounts"][acct]
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
            assert f.start is not None and f.end is not None
            return [c], call(
                "update_event_time", handle=h[key], start=iso(f.start, tz), end=iso(f.end, tz)
            )
        if isinstance(change, EventCancelledChange):
            key = (change.calendar_id, change.event_id)
            c, h = await self.expose_events([key])
            return [c], call("cancel_event", handle=h[key])
        if isinstance(change, DraftSentChange):
            assert change.reply_to_message_id is not None
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


def step(text: str | None = None, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "text": text,
        "tool_calls": calls or [],
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.0001,
    }


async def oracle_script(
    snapshot: Any, task: Task, *, runflow: bool, with_fault: bool = True
) -> list[dict[str, Any]]:
    """과제의 정답을 제출하는 스크립트 LLM 응답. `runflow`면 제출 뒤 마무리 응답을 하나 더 둔다."""
    o = Oracle(snapshot, task)
    changes = list(task.gold.expected_changes or [])
    steps: list[dict[str, Any]] = []
    structured: dict[str, Any] | None = None
    reads: list[dict[str, Any]] = []
    if task.gold.expected_answer is not None:
        reads, structured = await o.answer()
    pre: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    for change in changes:
        c, w = await o.write(change)
        pre += c
        writes.append(w)
    if len(task.turns) == 1:
        read_calls = reads or pre
        if read_calls:
            steps.append(step(calls=read_calls))
        if structured is not None:
            steps.append({**step(o.text()), "structured": structured})
            if runflow:
                steps.append(step("정리했습니다."))
        elif writes:
            steps.append(step(calls=writes))
            steps.append(step("처리했습니다."))
        else:
            steps.append(step(o.text()))
    else:
        steps += _multi_turn_steps(o, task, reads, pre, writes, structured, runflow)
    if with_fault and task.fault_plan:
        steps = _with_failed_first_read(steps, task)
    return steps


def _multi_turn_steps(
    o: Oracle,
    task: Task,
    reads: list[dict[str, Any]],
    pre: list[dict[str, Any]],
    writes: list[dict[str, Any]],
    structured: dict[str, Any] | None,
    runflow: bool,
) -> list[dict[str, Any]]:
    n = len(task.turns)
    if not runflow:  # assistant_v0는 모든 발화를 한 번에 받는다
        calls = reads or pre
        out = [step(calls=calls)] if calls else []
        out.append({**step(o.text()), "structured": structured} if structured else step(o.text()))
        return out
    out = []
    if structured is not None:  # 읽기 다중 턴: 첫 턴에서 조회하고 마지막 턴에서 제출한다
        out += [step(calls=reads), step("조회했습니다.")]
        out += [step("확인했습니다.") for _ in range(n - 2)]
        out += [{**step(o.text()), "structured": structured}, step("정리했습니다.")]
        return out
    out += [step(calls=pre), step("조회했습니다.")]  # 첫 턴: 대상을 찾는다
    out += [step("확인했습니다.") for _ in range(n - 2)]
    out += [step(calls=writes), step("처리했습니다.")]
    return out


def _with_failed_first_read(steps: list[dict[str, Any]], task: Task) -> list[dict[str, Any]]:
    """첫 조회 호출이 장애로 실패하므로 같은 호출 단계를 한 번 더 둔다(재시도)."""
    assert task.fault_plan is not None
    tool = task.fault_plan["rules"][0]["tool"]
    if tool not in ("list_events", "search_messages"):
        return steps
    for i, s in enumerate(steps):
        if any(c["name"] in ("list_events", "search_mail") for c in s["tool_calls"]):
            return [*steps[: i + 1], dict(s), *steps[i + 1 :]]
    return steps


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()
