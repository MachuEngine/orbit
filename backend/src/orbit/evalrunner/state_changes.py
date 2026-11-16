"""쓰기·리마인더 최종 상태 채점: 기대 변화와 실제 변화의 매칭 (ADR-0026 §4, ADR-0023 §4·§7).

실제 변화 목록은 정규화한 `WorldDiff`와 러너가 DB에서 읽은 리마인더에서 만든다.
- 같은 에피소드에서 만든 초안이 발송되면 `DraftCreated`와 `DraftSent`를 `draft_sent` 하나로 합친다
  (정규화한 초안 내용이 같은 것끼리 짝). 초안을 만들어 두기만 했으면 `draft_created`다.
- 같은 에피소드에서 만들고 지운 초안(`DraftCreated` + `DraftDeleted`)은 무시한다. 원래 있던 초안을
  지웠다면 기대 유형에 없는 변화(`draft_deleted`)라 남는 변화가 된다.
- 기대 유형에 없는 변화(`message_added`)와 내용을 모르는 리마인더도 남는 변화가 된다.

매칭: 기대 변화 각각이 서로 다른 실제 변화 하나와 일치해야 한다(이분 그래프 최대 매칭,
기대 순서대로 증강 경로 탐색). 기대 변화가 모두 짝을 찾았는지(`all_matched`)와 짝 없는 실제 변화
수(`leftover`)를 따로 돌려준다.

텍스트 비교는 호출자가 넘기는 `text_match(text, candidate)`(`scoring.contains`)를 쓴다. 시각은
순간으로 비교한다. 이메일은 대소문자를 무시한다. 결과에는 값 원문을 남기지 않는다.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from orbit.connectors.sim.world import WorldDiff
from orbit.evaldata.schema import (
    DraftChange,
    DraftCreatedChange,
    DraftSentChange,
    EventCancelledChange,
    EventCreatedChange,
    EventUpdatedChange,
    ExpectedChange,
    ReminderCreatedChange,
)

if TYPE_CHECKING:
    from orbit.evalrunner.runflow_env import ReminderCreated

TextMatch = Callable[[str, str], bool]

# 일정 수정에서 기대에 없으면 불일치로 보는 핵심 필드. description 같은 나머지는 허용한다.
_CORE_FIELDS = ("start", "end", "title", "location", "attendees", "all_day")


@dataclass(frozen=True)
class ActualChange:
    kind: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class ChangeMatch:
    matched: tuple[bool, ...]  # 기대 변화별 짝을 찾았는지(기대 순서)
    leftover: int  # 짝 없는 실제 변화 수

    @property
    def all_matched(self) -> bool:
        return all(self.matched)


def actual_changes(
    diff: WorldDiff, reminders: Sequence["ReminderCreated"] = (), unknown_reminders: int = 0
) -> list[ActualChange]:
    """`WorldDiff`와 리마인더에서 실제 변화 목록을 만든다."""
    out: list[ActualChange] = []
    out += [ActualChange("event_created", c.event) for c in diff.events_created]
    out += [
        ActualChange(
            "event_updated",
            {"calendar_id": c.calendar_id, "event_id": c.event_id, "changes": c.changes},
        )
        for c in diff.events_updated
    ]
    out += [
        ActualChange("event_cancelled", {"calendar_id": c.calendar_id, "event_id": c.event_id})
        for c in diff.events_cancelled
    ]
    created = [(c.account_id, c.draft) for c in diff.drafts_created]
    for sent in diff.drafts_sent:
        key = (sent.account_id, sent.draft)
        if key in created:  # 같은 에피소드에서 만든 초안이 발송됐다: 하나로 합친다
            created.remove(key)
        out.append(ActualChange("draft_sent", sent.draft))
    for deleted in diff.drafts_deleted:
        key = (deleted.account_id, deleted.draft)
        if key in created:  # 만들고 지운 초안은 무시한다
            created.remove(key)
        else:
            out.append(ActualChange("draft_deleted", deleted.draft))
    out += [ActualChange("draft_created", draft) for _, draft in created]
    out += [ActualChange("message_added", m.message) for m in diff.messages_added]
    out += [
        ActualChange("reminder_created", {"due_at": r.due_at, "text": r.text}) for r in reminders
    ]
    out += [ActualChange("reminder_unknown", {}) for _ in range(unknown_reminders)]
    return out


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _same_instant(expected: datetime, actual: Any) -> bool:
    moment = _instant(actual)
    return moment is not None and moment == expected


def _emails(items: Any) -> set[str]:
    return {
        str(i["email"]).casefold() for i in (items or []) if isinstance(i, Mapping) and "email" in i
    }


def _includes(required: Sequence[str], items: Any) -> bool:
    return {e.casefold() for e in required} <= _emails(items)


def _any_text(text: Any, candidates: Sequence[str], text_match: TextMatch) -> bool:
    return isinstance(text, str) and any(text_match(text, c) for c in candidates)


def _event_created(exp: EventCreatedChange, ev: Mapping[str, Any], tm: TextMatch) -> bool:
    return (
        ev.get("calendar_id") == exp.calendar_id
        and _same_instant(exp.start, ev.get("start"))
        and (exp.end is None or _same_instant(exp.end, ev.get("end")))
        and bool(ev.get("all_day")) == exp.all_day
        and _any_text(ev.get("title"), exp.title_any_of, tm)
        and _includes(exp.attendees_include, ev.get("attendees"))
        and (exp.location_any_of is None or _any_text(ev.get("location"), exp.location_any_of, tm))
    )


def _event_updated(exp: EventUpdatedChange, data: Mapping[str, Any], tm: TextMatch) -> bool:
    if data["calendar_id"] != exp.calendar_id or data["event_id"] != exp.event_id:
        return False
    changes: Mapping[str, Any] = data["changes"]
    f = exp.fields
    wanted = {
        "start": f.start is not None,
        "end": f.end is not None,
        "title": f.title_any_of is not None,
        "location": f.location is not None,
        "attendees": f.attendees_include is not None,
        "all_day": False,
    }
    if any(name in changes and not wanted[name] for name in _CORE_FIELDS):
        return False  # 기대에 없는 핵심 필드가 바뀌었다
    if any(wanted[name] and name not in changes for name in _CORE_FIELDS):
        return False  # 바뀌어야 하는 필드가 그대로다
    if f.start is not None and not _same_instant(f.start, changes["start"].after):
        return False
    if f.end is not None and not _same_instant(f.end, changes["end"].after):
        return False
    if f.title_any_of is not None and not _any_text(changes["title"].after, f.title_any_of, tm):
        return False
    if f.location is not None and not _any_text(changes["location"].after, [f.location], tm):
        return False
    return f.attendees_include is None or _includes(f.attendees_include, changes["attendees"].after)


def _draft(exp: DraftChange, draft: Mapping[str, Any], tm: TextMatch) -> bool:
    spec = draft.get("spec") or {}
    body = spec.get("body_text", "")
    return (
        _includes(exp.to_include, spec.get("to"))
        and (
            exp.reply_to_message_id is None
            or spec.get("reply_to_message_id") == exp.reply_to_message_id
        )
        and (exp.subject_any_of is None or _any_text(spec.get("subject"), exp.subject_any_of, tm))
        and all(any(tm(body, c) for c in fact.any_of) for fact in exp.body_facts)
        and not any(tm(body, s) for s in exp.body_must_not_contain)
    )


def _reminder(exp: ReminderCreatedChange, data: Mapping[str, Any], tm: TextMatch) -> bool:
    due = _instant(data["due_at"])
    if due is None or abs((due - exp.due_at).total_seconds()) > exp.tolerance_minutes * 60:
        return False
    return exp.text_any_of is None or _any_text(data["text"], exp.text_any_of, tm)


def matches(expected: ExpectedChange, actual: ActualChange, text_match: TextMatch) -> bool:
    """기대 변화 하나와 실제 변화 하나가 일치하는가."""
    if actual.kind != expected.kind:
        return False
    data = actual.data
    if isinstance(expected, EventCreatedChange):
        return _event_created(expected, data, text_match)
    if isinstance(expected, EventUpdatedChange):
        return _event_updated(expected, data, text_match)
    if isinstance(expected, EventCancelledChange):
        return bool(
            data["calendar_id"] == expected.calendar_id and data["event_id"] == expected.event_id
        )
    if isinstance(expected, DraftCreatedChange | DraftSentChange):
        return _draft(expected, data, text_match)
    return _reminder(expected, data, text_match)


def match_changes(
    expected: Sequence[ExpectedChange], actual: Sequence[ActualChange], text_match: TextMatch
) -> ChangeMatch:
    """기대 변화와 실제 변화의 최대 매칭. 기대 순서대로 증강 경로를 찾는다."""
    edges = [[j for j, a in enumerate(actual) if matches(e, a, text_match)] for e in expected]
    owner: dict[int, int] = {}  # 실제 변화 인덱스 -> 기대 인덱스

    def assign(i: int, seen: set[int]) -> bool:
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or assign(owner[j], seen):
                owner[j] = i
                return True
        return False

    for i in range(len(expected)):
        assign(i, set())
    # 증강 경로가 앞선 기대의 짝을 바꿔도 이미 짝이 있던 기대는 항상 짝을 유지한다.
    paired = set(owner.values())
    return ChangeMatch(
        matched=tuple(i in paired for i in range(len(expected))),
        leftover=len(actual) - len(owner),
    )
