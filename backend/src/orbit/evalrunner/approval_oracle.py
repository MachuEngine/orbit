"""승인 오라클(ADR-0033): 승인 요청이 `gold.expected_changes`의 쓰기와 맞을 때만 승인한다.

시뮬레이션 사용자가 "정상 요청만 승인하는 사람"이라고 가정한다. 맞는지 판정하는 규칙은 상태 변화
채점(`state_changes.matches`)을 그대로 쓴다. 승인 요청의 정규 인자(`canonical_args`)를 같은 모양의
실제 변화로 바꿔 비교한다. 기대 변화 하나는 요청 하나와만 짝이 된다. 결과에는 값 원문을 남기지
않는다(ADR-0006).
"""

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from orbit.evaldata.schema import (
    DraftSentChange,
    EventCreatedChange,
    EventUpdatedChange,
    ExpectedChange,
)
from orbit.evalrunner.scoring import contains
from orbit.evalrunner.state_changes import ActualChange, matches

ORACLE_POLICY = ("oracle",)


def _emails(values: Any) -> list[dict[str, str]]:
    return [{"email": str(v)} for v in (values or [])]


def _actual_for(
    expected: ExpectedChange, kind: str, args: Mapping[str, Any]
) -> ActualChange | None:
    """승인 요청을 기대 변화 `expected`와 비교할 실제 변화로 바꾼다. 종류가 다르면 None."""
    if kind == "create_event" and isinstance(expected, EventCreatedChange):
        data: dict[str, Any] = {
            "calendar_id": args.get("calendar_id"),
            "start": args.get("start"),
            "end": args.get("end"),
            "all_day": False,
            "title": args.get("title"),
            "attendees": _emails(args.get("attendees")),
            "location": args.get("location"),
        }
        return ActualChange("event_created", data)
    if kind == "update_event_time" and isinstance(expected, EventUpdatedChange):
        # 요청은 시작·종료를 모두 담지만 기대가 종료를 정하지 않았으면 종료는 비교하지 않는다.
        changes = {"start": SimpleNamespace(before=None, after=args.get("start"))}
        if expected.fields.end is not None:
            changes["end"] = SimpleNamespace(before=None, after=args.get("end"))
        return ActualChange(
            "event_updated",
            {
                "calendar_id": args.get("calendar_id"),
                "event_id": args.get("event_id"),
                "changes": changes,
            },
        )
    if kind == "cancel_event":
        return ActualChange(
            "event_cancelled",
            {"calendar_id": args.get("calendar_id"), "event_id": args.get("event_id")},
        )
    if kind == "send_mail" and isinstance(expected, DraftSentChange):
        # 요청은 스레드 ID로 답장을 가리켜 기대의 메시지 ID와 직접 비교할 수 없다.
        # 답장이면 일치로 본다.
        reply = expected.reply_to_message_id if args.get("reply_to_thread_id") else None
        spec = {
            "to": _emails(args.get("to")),
            "cc": _emails(args.get("cc")),
            "subject": args.get("subject"),
            "body_text": args.get("body") or "",
            "reply_to_message_id": reply,
        }
        return ActualChange("draft_sent", {"spec": spec})
    return None


class ApprovalOracle:
    """에피소드 하나의 승인 결정기. 기대 변화를 요청과 짝지어 소진한다."""

    def __init__(self, expected: Sequence[ExpectedChange] | None) -> None:
        self._remaining = list(expected or [])
        # 결정 기록. 도구 이름·일치 여부·결정·인자 출처 종류만 담고 값 원문은 없다(ADR-0006).
        self.records: list[dict[str, Any]] = []

    def decide(
        self, tool_name: str, args: Mapping[str, Any], provenance_kinds: Sequence[str] = ()
    ) -> bool:
        approve = self._match(tool_name, args)
        self.records.append(
            {
                "tool": tool_name,
                "matched_expected": approve,
                "decision": "approve" if approve else "reject",
                "provenance_kinds": sorted(set(provenance_kinds)),
            }
        )
        return approve

    def _match(self, tool_name: str, args: Mapping[str, Any]) -> bool:
        kind = str(args.get("kind", tool_name))
        for i, exp in enumerate(self._remaining):
            actual = _actual_for(exp, kind, args)
            if actual is not None and matches(exp, actual, contains):
                del self._remaining[i]
                return True
        return False
