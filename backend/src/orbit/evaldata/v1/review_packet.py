"""검수 꾸러미: 문항과 정답을 사람이 읽는 한국어 텍스트로 만든다 (ADR-0026 §8).

자동 검수(LLM)와 사람 검수 표본이 같은 꾸러미를 쓴다. 두 용도는 본문 발췌 길이만 다르다.
정답은 `gold`의 내부 ID가 아니라 세계에서 찾은 일정 제목·현지 시각, 메일 제목·발신자·발췌,
문서 이름·발췌로 풀어 쓴다. 이 모듈은 LLM을 부르지 않고 파일을 쓰지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from orbit.connectors.contracts import CalendarEvent
from orbit.evaldata.schema import (
    DEFAULT_TIME_ZONE,
    AbstainAnswer,
    ClarifyAnswer,
    DocumentAnswer,
    DraftChange,
    DraftSentChange,
    EventAnswer,
    EventCancelledChange,
    EventCreatedChange,
    EventSetAnswer,
    EventTimeAnswer,
    EventUpdatedChange,
    MessageSetAnswer,
    Task,
    TimeSlotAnswer,
)
from orbit.evaldata.timeutil import weekday_ko
from orbit.evaldata.v1.task_world import AccountView, TaskWorld

LLM_EXCERPT_CHARS = 1200  # 자동 검수에 보내는 근거 본문 발췌 상한
HUMAN_EXCERPT_CHARS = 300  # 사람 검수(폰) 꾸러미의 발췌 상한
MAX_EVIDENCE_ITEMS = 6
SEARCH_MAILBOX = "200"  # 검색 질의의 근거 문서를 읽는 우편함(크기가 가장 작은 것)

FORM_LABELS = {
    "keyword": "키워드형",
    "paraphrase_question": "의미를 바꿔 쓴 질문형",
    "person_time": "사람·시간형",
}

_SPACE = re.compile(r"\s+")


def task_type(task: Task) -> str:
    return next((t.split(":", 1)[1] for t in task.tags if t.startswith("type:")), "?")


def task_origin(task: Task) -> str | None:
    """파생 과제(fault·paraphrase)의 원본 과제 ID. 원본이면 None."""
    if "derived:none" in task.tags:
        return None
    origin = task.difficulty.get("derived_from")
    return str(origin) if origin is not None else None


def task_family(tw: TaskWorld, task: Task) -> str:
    for sc in tw.skeleton.scenarios:
        if sc.scenario_id == task.scenario_id:
            return str(sc.generator_family)
    return next((t.split(":", 1)[1] for t in task.tags if t.startswith("family:")), "?")


def scenario_family(tw: TaskWorld, scenario_id: str) -> str:
    for sc in tw.skeleton.scenarios:
        if sc.scenario_id == scenario_id:
            return str(sc.generator_family)
    return "?"


# ---------------------------------------------------------------- 표기 도우미


def _zone(name: str | None) -> ZoneInfo:
    return ZoneInfo(name or DEFAULT_TIME_ZONE)


def local_text(moment: datetime, tz: tzinfo) -> str:
    """현지 표기. 예: `2026-11-16(월) 14:30`."""
    local = moment.astimezone(tz)
    return f"{local:%Y-%m-%d}({weekday_ko(local.date())}) {local:%H:%M}"


def _time_only(moment: datetime, tz: tzinfo) -> str:
    return f"{moment.astimezone(tz):%H:%M}"


def _day_text(day: date) -> str:
    return f"{day:%Y-%m-%d}({weekday_ko(day)})"


def excerpt(text: str, limit: int, focus: Sequence[str] = ()) -> str:
    """공백을 정리하고 `limit`자 안으로 자른다. `focus` 표현이 앞쪽에 없으면 그 근처부터 보인다."""
    flat = _SPACE.sub(" ", text).strip()
    if len(flat) <= limit:
        return flat
    start = 0
    positions = [flat.find(f) for f in focus if f and f in flat]
    if positions and min(positions) > limit * 0.6:
        start = max(0, min(positions) - limit // 3)
    piece = flat[start : start + limit].strip()
    return ("…" if start > 0 else "") + piece + ("…" if start + limit < len(flat) else "")


def _person(p: dict[str, Any] | None) -> str:
    if not p:
        return "?"
    return str(p.get("name") or p.get("email") or "?")


# ---------------------------------------------------------------- 아티팩트 풀이


def event_text(e: CalendarEvent, tz: tzinfo) -> str:
    when = (
        f"{_day_text(e.start.astimezone(tz).date())} 종일"
        if e.all_day
        else f"{local_text(e.start, tz)}~{_time_only(e.end, tz)}"
    )
    extras = []
    if e.status == "cancelled":
        extras.append("취소됨")
    if e.location:
        extras.append(f"장소 {e.location}")
    people = ", ".join(a.name or a.email for a in e.attendees)
    if people:
        extras.append(f"참석 {people}")
    tail = f" ({'; '.join(extras)})" if extras else ""
    return f'일정 "{e.title}" {when}{tail}'


def _message_text(
    m: dict[str, Any],
    tz: tzinfo,
    limit: int,
    focus: Sequence[str],
    *,
    with_email: bool = False,
) -> str:
    sent = datetime.fromisoformat(str(m["sent_at"]).replace("Z", "+00:00"))
    body = excerpt(str(m.get("body_text") or m.get("snippet") or ""), limit, focus)
    sender = _person(m.get("from"))
    address = (m.get("from") or {}).get("email")
    if with_email and address:
        sender = f"{sender} <{address}>"
    return f'메일 "{m.get("subject", "")}" 발신 {sender} {local_text(sent, tz)}\n    본문: {body}'


def _file_text(
    f: dict[str, Any], tz: tzinfo, limit: int, focus: Sequence[str], *, with_owner: bool = False
) -> str:
    modified = datetime.fromisoformat(str(f["modified_at"]).replace("Z", "+00:00"))
    body = excerpt(str(f.get("text") or ""), limit, focus)
    owner = ""
    if with_owner:
        names = ", ".join(_person(o) for o in f.get("owners") or []) or "?"
        owner = f" 소유자 {names}"
    return f'문서 "{f.get("name", "")}"{owner} 수정 {local_text(modified, tz)}\n    본문: {body}'


class _Ctx:
    def __init__(self, acct: AccountView, tz: tzinfo, limit: int, focus: Sequence[str]) -> None:
        self.acct = acct
        self.tz = tz
        self.limit = limit
        self.focus = focus
        self.shown: set[str] = set()
        self.gold_refs: dict[
            str, str | None
        ] = {}  # 정답·기대 변화가 가리킨 근거: 키 -> calendar_id
        self.events_by_id = {e.id: e for e in acct.events}
        self._record_gold = False

    def recording_gold(self) -> _Ctx:
        """이후 `event`·`message`·`file` 호출을 정답 근거로 기록한다."""
        self._record_gold = True
        return self

    def stop_recording(self) -> None:
        self._record_gold = False

    def _note(self, key: str, calendar_id: str | None = None) -> None:
        self.shown.add(key)
        if self._record_gold:
            self.gold_refs.setdefault(key, calendar_id)

    def calendar_zone(self, calendar_id: str | None, event_id: str) -> str | None:
        found = self.acct.event(calendar_id or "", event_id) or self.events_by_id.get(event_id)
        cal = found.calendar_id if found else calendar_id
        for c in self.acct.calendars:
            if c.get("id") == cal and c.get("time_zone"):
                return str(c["time_zone"])
        return None

    def event(self, calendar_id: str | None, event_id: str) -> str:
        found = self.acct.event(calendar_id or "", event_id) or self.events_by_id.get(event_id)
        self._note(f"ref_event:{event_id}", calendar_id)
        return event_text(found, self.tz) if found else f"일정(세계에 없음: {event_id})"

    def message(self, message_id: str, *, with_email: bool = False) -> str:
        self._note(f"ref_message:{message_id}")
        m = self.acct.messages.get(message_id)
        if m is None:
            return f"메일(세계에 없음: {message_id})"
        return _message_text(m, self.tz, self.limit, self.focus, with_email=with_email)

    def file(self, file_id: str) -> str:
        self._note(f"ref_file:{file_id}")
        f = self.acct.files.get(file_id)
        if f is None:
            return f"문서(세계에 없음: {file_id})"
        return _file_text(f, self.tz, self.limit, self.focus)


# ---------------------------------------------------------------- 정답 풀이


def _answer_lines(answer: Any, ctx: _Ctx) -> list[str]:
    if isinstance(answer, EventSetAnswer):
        if not answer.events:
            return ["- 해당하는 일정이 하나도 없다(빈 집합)"]
        return [f"- {ctx.event(e.calendar_id, e.event_id)}" for e in answer.events]
    if isinstance(answer, EventAnswer):
        return [f"- {ctx.event(answer.event.calendar_id, answer.event.event_id)}"]
    if isinstance(answer, EventTimeAnswer):
        e = ctx.event(answer.event.calendar_id, answer.event.event_id)
        return [f"- {e}", f"  정답 시작 시각: {local_text(answer.start, ctx.tz)}"]
    if isinstance(answer, TimeSlotAnswer):
        return [f"- 빈 시간 시작: {local_text(answer.start, ctx.tz)}"]
    if isinstance(answer, MessageSetAnswer):
        if not answer.messages:
            return ["- 해당하는 메일이 하나도 없다(빈 집합)"]
        return [f"- {ctx.message(m.message_id)}" for m in answer.messages]
    if isinstance(answer, DocumentAnswer):
        return [f"- {ctx.file(answer.document.file_id)}"]
    return [f"- (알 수 없는 정답 형식 {type(answer).__name__})"]


def _expected_change_lines(task: Task, ctx: _Ctx) -> list[str]:
    lines: list[str] = []
    tz = ctx.tz
    for ch in task.gold.expected_changes or []:
        if isinstance(ch, EventCreatedChange):
            who = ", ".join(ch.attendees_include) or "없음"
            loc = f", 장소 {ch.location_any_of[0]}" if ch.location_any_of else ""
            span_text = (
                f"{local_text(ch.start, tz)}~{_time_only(ch.end, tz)}"
                if ch.end is not None
                else f"{local_text(ch.start, tz)} 시작(종료 시각은 채점하지 않음)"
            )
            lines.append(
                f'- 일정 생성: "{ch.title_any_of[0]}" {span_text}'
                f"{' 종일' if ch.all_day else ''}, 참석 {who}{loc}"
            )
        elif isinstance(ch, EventUpdatedChange):
            parts = []
            f = ch.fields
            if f.start is not None:
                parts.append(f"시작 {local_text(f.start, tz)}")
            if f.end is not None:
                parts.append(f"종료 {local_text(f.end, tz)}")
            if f.location is not None:
                parts.append(f"장소 {f.location}")
            if f.title_any_of is not None:
                parts.append(f"제목 {f.title_any_of[0]}")
            if f.attendees_include is not None:
                parts.append("참석자 추가 " + ", ".join(f.attendees_include))
            note = _event_length_note(ctx, ch) if f.start is not None and f.end is not None else ""
            lines.append(
                f"- 일정 변경: {ctx.event(ch.calendar_id, ch.event_id)} -> {'; '.join(parts)}{note}"
            )
        elif isinstance(ch, EventCancelledChange):
            lines.append(f"- 일정 취소: {ctx.event(ch.calendar_id, ch.event_id)}")
        elif isinstance(ch, DraftChange):
            label = "답장 발송" if isinstance(ch, DraftSentChange) else "답장 초안 작성"
            subj = f', 제목 "{ch.subject_any_of[0]}"' if ch.subject_any_of else ""
            facts = "; ".join(f"{x.description}={x.any_of[0]}" for x in ch.body_facts)
            graded = (
                f"수신자·답장 대상 + 답장 본문 사실: {facts}"
                if facts
                else "수신자·답장 대상만 채점, 답장 본문 사실: 없음"
            )
            lines.append(f"- {label}: 수신 {', '.join(ch.to_include)}{subj}")
            lines.append(f"    채점 항목: {graded} (그 밖의 답장 문구는 채점하지 않는다)")
            if ch.reply_to_message_id:
                lines.append(
                    "    답장 대상 메일(사용자가 받은 메일이며 기대 답장 본문이 아니다): "
                    + ctx.message(ch.reply_to_message_id, with_email=True)
                )
        else:
            text = f', 내용 "{ch.text_any_of[0]}"' if ch.text_any_of else ""
            lines.append(
                f"- 리마인더 생성: {local_text(ch.due_at, tz)} (허용 오차 {ch.tolerance_minutes}분)"
                f"{text}"
            )
    return lines


def _event_length_note(ctx: _Ctx, ch: EventUpdatedChange) -> str:
    """기대 변경의 시작·종료가 둘 다 있을 때, 원래 일정 길이를 유지한 값인지 밝힌다."""
    f = ch.fields
    if f.start is None or f.end is None:
        return ""
    found = ctx.acct.event(ch.calendar_id or "", ch.event_id) or ctx.events_by_id.get(ch.event_id)
    minutes = int((f.end - f.start).total_seconds() // 60)
    kept = found is not None and int((found.end - found.start).total_seconds() // 60) == minutes
    suffix = f" (원래 길이 {minutes}분 유지)" if kept else f" (길이 {minutes}분)"
    return suffix + " [규칙: 메일에 종료 시각이 없으면 원래 일정 길이를 유지한 값이 정답]"


def _clarify_lines(answer: ClarifyAnswer, ctx: _Ctx, labels: Sequence[str] = ()) -> list[str]:
    lines = ["- 질문이 모호해 되물어야 한다. 가능한 해석과 각 해석의 정답:"]
    for i, reading in enumerate(answer.readings, start=1):
        sub = _answer_lines(reading, ctx)
        name = f" [{labels[i - 1]}]" if len(labels) == len(answer.readings) else ""
        lines.append(f"  해석 {i}{name} ({reading.kind}):")
        lines += [f"  {x}" for x in sub]
    return lines


MISMATCH_RULE = (
    "규칙: 메일과 캘린더가 어긋나면 캘린더에 잡힌 값이 정답이다"
    "(질문은 캘린더 값을 묻는다, ADR-0026)"
)


def _scenario_type(tw: TaskWorld, task: Task) -> str:
    for sc in tw.skeleton.scenarios:
        if sc.scenario_id == task.scenario_id:
            return str(sc.type)
    return next((t.split(":", 1)[1] for t in task.tags if t.startswith("scenario:")), "?")


def _homonym_labels(tw: TaskWorld, task: Task) -> list[str]:
    """동명이인 모호성의 해석 label(예: "최유나(새봄디자인)"). 다른 모호성 유형은 빈 목록."""
    for sc in tw.skeleton.scenarios:
        if sc.scenario_id == task.scenario_id:
            truth = sc.truth.model_dump(mode="json")
            if truth.get("variant") != "homonym_person":
                return []
            return [str(i["label"]) for i in truth.get("interpretations", [])]
    return []


def _abstain_target(tw: TaskWorld, task: Task) -> str | None:
    for sc in tw.skeleton.scenarios:
        if sc.scenario_id == task.scenario_id:
            missing = sc.truth.model_dump(mode="json").get("missing")
            return str(missing) if missing else None
    return None


# ---------------------------------------------------------------- 꾸러미


def task_packet(tw: TaskWorld, task: Task, *, excerpt_chars: int = LLM_EXCERPT_CHARS) -> str:
    """종단 과제 하나의 검수 꾸러미. 질문 전체와 정답, 근거를 사람이 읽는 형태로 쓴다."""
    return _task_packet(tw, task, excerpt_chars)[0]


def task_evidence_keys(tw: TaskWorld, task: Task) -> list[str]:
    """꾸러미 근거 자료에 실린 아티팩트 키(`ref_event:ID` 등). 정답 근거 누락 검사에 쓴다."""
    return _task_packet(tw, task, LLM_EXCERPT_CHARS)[1]


def _task_packet(tw: TaskWorld, task: Task, excerpt_chars: int) -> tuple[str, list[str]]:
    acct = tw.account(task.account_id)
    tz = _zone(task.time_zone or acct.time_zone)
    focus = [x for f in task.gold.answer_facts for x in f.any_of[:1]]
    ctx = _Ctx(acct, tz, excerpt_chars, focus)
    lines = [
        f"[종단 과제 {task.task_id} | 유형 {task_type(task)} | 시나리오 {task.scenario_id}]",
        f"사용자: {acct.name} (시간대 {tz.key if isinstance(tz, ZoneInfo) else tz})",
        f"기준 시각(as_of): {local_text(task.as_of_time, tz)}",
        "질문(사용자 발화):",
    ]
    if len(task.turns) == 1:
        lines.append(f"  {task.turns[0].content}")
    else:
        lines += [f"  턴{i}: {t.content}" for i, t in enumerate(task.turns, start=1)]
    if _scenario_type(tw, task) == "mismatch":
        lines.append(MISMATCH_RULE)
    if task.approval_policy and "reject" in task.approval_policy:
        lines.append("(시뮬레이션 사용자가 쓰기 승인을 거절하는 과제)")
    answer = task.gold.expected_answer
    gold_lines: list[str] = []
    ctx.recording_gold()
    if answer is None:
        pass
    elif isinstance(answer, AbstainAnswer):
        target = _abstain_target(tw, task)
        gold_lines.append(
            "- 기권이 정답이다: 도구로 확인해도 답할 정보가 세계에 없다"
            + (f" (없는 대상: {target})" if target else "")
        )
    elif isinstance(answer, ClarifyAnswer):
        gold_lines += _clarify_lines(answer, ctx, _homonym_labels(tw, task))
    else:
        gold_lines += _answer_lines(answer, ctx)
    for f in task.gold.answer_facts:
        more = f" (허용 표현 {len(f.any_of)}개)" if len(f.any_of) > 1 else ""
        gold_lines.append(f"- 정답 값 {f.description}: {' / '.join(f.any_of[:2])}{more}")
    if gold_lines:
        lines += ["정답:", *gold_lines]
    changes = _expected_change_lines(task, ctx)
    ctx.stop_recording()
    if changes:
        lines.append("쓰기 기대 변화:")
        lines += changes
    elif not gold_lines:
        lines.append("정답: 상태 변화 없음(쓰기 요청이 거절되어 아무것도 바뀌지 않아야 한다)")
    if task.gold.must_not_contain:
        lines.append("응답에 있으면 안 되는 표현: " + ", ".join(task.gold.must_not_contain))
    evidence_keys, evidence = _evidence_lines(tw, task, ctx)
    lines += evidence
    return "\n".join(lines), evidence_keys


# 시나리오 진실에서 정답 계산에 쓰인 아티팩트를 가리키는 필드 -> 표시 이름
_TRUTH_ROLES = {
    "mail_fact": {"latest_message": "최신 값 메일", "previous_message": "이전 값 메일"},
    "mismatch": {"event": "캘린더 일정", "message": "일정 안내 메일(캘린더와 어긋남)"},
}
_TRUTH_ROLES_ANY = {
    "change_message": "변경 요청 메일",
    "cancel_message": "취소 안내 메일",
    "request_message": "요청 메일",
    "awaiting_message": "답을 기다리는 메일",
    "message": "근거 메일",
    "latest_file": "최신 문서",
    "previous_file": "이전 문서",
    "file": "근거 문서",
    "source": "근거 문서",
}


def _truth_refs(tw: TaskWorld, task: Task) -> list[tuple[str, str | None, str]]:
    """(키, calendar_id, 역할 이름). 정답 계산에 쓰인 아티팩트를 시나리오 진실에서 모은다."""
    for sc in tw.skeleton.scenarios:
        if sc.scenario_id != task.scenario_id:
            continue
        truth = sc.truth.model_dump(mode="json")
        roles = _TRUTH_ROLES.get(str(truth.get("type")), _TRUTH_ROLES_ANY)
        found: list[tuple[str, str | None, str]] = []
        for field, label in roles.items():
            ref = truth.get(field)
            if isinstance(ref, dict) and ref.get("kind") in {"event", "message", "file"}:
                found.append((f"ref_{ref['kind']}:{ref['id']}", ref.get("calendar_id"), label))
        return found
    return []


def _evidence_lines(tw: TaskWorld, task: Task, ctx: _Ctx) -> tuple[list[str], list[str]]:
    """근거 자료. 정답 근거가 먼저고 한 건도 자르지 않는다. 나머지는 상한까지만 보인다."""
    roles: dict[str, str] = {}
    cal_of: dict[str, str | None] = dict(ctx.gold_refs)
    ordered: list[str] = list(ctx.gold_refs)
    for key, cal, label in _truth_refs(tw, task):
        roles[key] = label
        cal_of.setdefault(key, cal)
        if key not in ordered:
            ordered.append(key)
    others = [t for t in task.tags if t.startswith(("ref_event:", "ref_message:", "ref_file:"))]
    rest = [t for t in others if t not in ordered]
    budget = max(MAX_EVIDENCE_ITEMS - len(ordered), 0)
    shown_rest = rest[:budget]
    ordered += shown_rest
    if not ordered:
        return [], []
    lines = ["근거 자료:"]
    for key in ordered:
        kind, ident = key.split(":", 1)
        if kind == "ref_event":
            body = ctx.event(cal_of.get(key), ident)
            zone = ctx.calendar_zone(cal_of.get(key), ident)
            if zone:
                body += f" [캘린더 시간대 {zone}]"
        elif kind == "ref_message":
            body = ctx.message(ident)
        else:
            body = ctx.file(ident)
        prefix = "(정답 근거) " if key in ctx.gold_refs else ""
        if key in roles:
            prefix += f"[{roles[key]}] "
        lines.append(f"- {prefix}{body}")
    if len(rest) > len(shown_rest):
        lines.append(f"- (정답과 직접 관계없는 근거 {len(rest) - len(shown_rest)}건 생략)")
    return ordered, lines


def search_item_text(
    acct: AccountView,
    item_id: str,
    tz: tzinfo,
    limit: int,
    focus: Sequence[str] = (),
    *,
    with_meta: bool = False,
) -> str:
    """`with_meta`면 메일 발신자 주소와 문서 소유자를 보인다(사람·시간형 질의 검수에 필요하다)."""
    if item_id in acct.messages:
        return _message_text(acct.messages[item_id], tz, limit, focus, with_email=with_meta)
    if item_id in acct.files:
        return _file_text(acct.files[item_id], tz, limit, focus, with_owner=with_meta)
    return f"(세계에 없는 문서 {item_id})"


def search_packet(
    tw: TaskWorld, row: dict[str, Any], *, excerpt_chars: int = LLM_EXCERPT_CHARS
) -> str:
    """검색 질의 하나의 검수 꾸러미: 질의, 등급 2·1 문서 발췌, 질의 형태."""
    mailbox = row["mailboxes"][SEARCH_MAILBOX]
    acct = tw.account(str(mailbox["account_id"]))
    as_of = datetime.fromisoformat(str(row["as_of"]).replace("Z", "+00:00"))
    tz = as_of.tzinfo or _zone(None)
    relevance: dict[str, int] = mailbox["relevance"]
    lines = [
        f"[검색 질의 {row['query_id']} | 형태 {FORM_LABELS.get(row['form'], row['form'])}]",
        f"질의: {row['query']}",
        f"기준 시각(as_of): {local_text(as_of, tz)}",
        "등급 2 문서(질의가 찾아야 할 정답 문서):",
    ]
    for grade, title in (
        (2, None),
        (1, "등급 1 문서(부분 관련: 같은 주제이지만 질의의 답은 아님):"),
    ):
        if title:
            lines.append(title)
        ids = sorted(i for i, g in relevance.items() if g == grade)
        if not ids:
            lines.append("- (없음)")
        for item_id in ids[:2]:
            meta = row["form"] == "person_time"
            lines.append(f"- {search_item_text(acct, item_id, tz, excerpt_chars, with_meta=meta)}")
        if len(ids) > 2:
            lines.append(f"- (외 {len(ids) - 2}건 생략)")
    return "\n".join(lines)
