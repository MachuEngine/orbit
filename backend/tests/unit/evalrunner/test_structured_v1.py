"""스위트 v1 정답 유형 채점(ADR-0026 §4·§7). 데이터는 모두 합성 값이다."""

from typing import Any

import pytest

from orbit.evaldata.schema import (
    AbstainAnswer,
    ClarifyAnswer,
    DocumentAnswer,
    DocumentRef,
    EventAnswer,
    EventRef,
    EventSetAnswer,
    EventTimeAnswer,
    MessageRef,
    MessageSetAnswer,
    TimeSlotAnswer,
)
from orbit.evalrunner.structured import answer_ids, check_structured, grade_structured

E1 = {"calendar_id": "c1", "event_id": "e1"}
E2 = {"calendar_id": "c1", "event_id": "e2"}
REF1 = EventRef(calendar_id="c1", event_id="e1")
REF2 = EventRef(calendar_id="c1", event_id="e2")
START = "2026-11-12T10:00:00+09:00"

MESSAGES = MessageSetAnswer(messages=[MessageRef(message_id="m1"), MessageRef(message_id="m2")])
DOCUMENT = DocumentAnswer(document=DocumentRef(file_id="f1"))
ABSTAIN = AbstainAnswer()
CLARIFY = ClarifyAnswer(
    readings=[EventAnswer(event=REF1), EventSetAnswer(events=[REF1, REF2])],
)


def msgs(*ids: str) -> dict[str, Any]:
    return {"kind": "message_set", "messages": [{"message_id": i} for i in ids]}


def doc(file_id: str) -> dict[str, Any]:
    return {"kind": "document", "document": {"file_id": file_id}}


def test_message_set_is_order_free() -> None:
    assert check_structured(MESSAGES, msgs("m2", "m1")) == "ok"
    assert check_structured(MESSAGES, msgs("m1")) == "wrong_answer"
    assert check_structured(MESSAGES, msgs("m1", "m2", "m3")) == "wrong_answer"
    assert check_structured(MessageSetAnswer(messages=[]), msgs()) == "ok"


@pytest.mark.parametrize(
    "structured",
    [
        {"kind": "message_set"},  # 필드 누락
        {"kind": "message_set", "messages": "m1"},
        {"kind": "message_set", "messages": ["m1"]},  # 문자열 항목
        {"kind": "message_set", "messages": [{"message_id": 1}]},
        {"kind": "message_set", "messages": [{"message_id": ""}]},
        {"kind": "message_set", "messages": [{"message_id": "m1", "x": 1}]},
        {**msgs("m1"), "note": "x"},  # 추가 키
        msgs("m1", "m1"),  # 중복 항목
        {"kind": "mystery"},  # 알 수 없는 kind
        {"kind": ["message_set"]},
        {},
        None,
        "문자열",
    ],
)
def test_message_set_format_errors(structured: Any) -> None:
    assert check_structured(MESSAGES, structured) == "answer_format"


def test_document() -> None:
    assert check_structured(DOCUMENT, doc("f1")) == "ok"
    assert check_structured(DOCUMENT, doc("f2")) == "wrong_answer"
    for bad in (
        {"kind": "document"},
        {"kind": "document", "document": "f1"},
        {"kind": "document", "document": {"file_id": 3}},
        {"kind": "document", "document": {"file_id": "f1", "x": 1}},
        {**doc("f1"), "x": 1},
    ):
        assert check_structured(DOCUMENT, bad) == "answer_format"


def test_abstain() -> None:
    assert check_structured(ABSTAIN, {"kind": "abstain"}) == "ok"
    assert check_structured(ABSTAIN, {"kind": "abstain", "x": 1}) == "answer_format"
    # 다른 유효한 답은 wrong_answer다(대상이 없어서 answer_kind가 될 수 없다)
    assert check_structured(ABSTAIN, doc("f1")) == "wrong_answer"
    assert check_structured(ABSTAIN, {"kind": "event_set", "events": []}) == "wrong_answer"
    assert check_structured(ABSTAIN, {"kind": "event_set", "events": [E1]}) == "wrong_answer"


def test_abstain_submitted_for_a_normal_question_is_wrong_answer() -> None:
    event = EventAnswer(event=REF1)
    assert check_structured(event, {"kind": "abstain"}) == "wrong_answer"
    # 빈 목록이 정답일 때도 abstain은 다른 종류의 답이고 대상이 비어 있어 answer_kind가 아니다
    assert check_structured(EventSetAnswer(events=[]), {"kind": "abstain"}) == "wrong_answer"


def test_kind_only_wrong_is_answer_kind() -> None:
    event_time = EventTimeAnswer(event=REF1, start="2026-11-12T01:00:00+00:00")
    # event_time 정답에 같은 일정을 event로 냈다(실측에서 가장 흔한 실수)
    assert check_structured(event_time, {"kind": "event", "event": E1}) == "answer_kind"
    # 한 개짜리 목록 <-> 단일 일정
    one = EventSetAnswer(events=[REF1])
    assert check_structured(one, {"kind": "event", "event": E1}) == "answer_kind"
    assert check_structured(EventAnswer(event=REF1), {"kind": "event_set", "events": [E1]}) == (
        "answer_kind"
    )
    # 메일 한 통 집합 <-> 문서는 대상 종류가 달라 같은 집합이 아니다
    one_msg = MessageSetAnswer(messages=[MessageRef(message_id="m1")])
    assert check_structured(one_msg, doc("m1")) == "wrong_answer"


def test_kind_and_target_both_wrong_is_wrong_answer() -> None:
    event_time = EventTimeAnswer(event=REF1, start="2026-11-12T01:00:00+00:00")
    assert check_structured(event_time, {"kind": "event", "event": E2}) == "wrong_answer"
    # 인물 다음 미팅에 빈 목록(ADR-0026 §7): 대상이 비어 있어 answer_kind가 아니다
    assert check_structured(EventAnswer(event=REF1), {"kind": "event_set", "events": []}) == (
        "wrong_answer"
    )
    assert check_structured(EventAnswer(event=REF1), {"kind": "event_set", "events": [E1, E2]}) == (
        "wrong_answer"
    )


def test_time_slot_has_no_targets_so_kind_mismatch_is_wrong_answer() -> None:
    slot = TimeSlotAnswer(start="2026-11-12T10:00:00+09:00")
    assert check_structured(slot, {"kind": "event", "event": E1}) == "wrong_answer"
    event = EventAnswer(event=REF1)
    assert check_structured(event, {"kind": "time_slot", "start": START}) == "wrong_answer"


def test_kind_mismatch_with_invalid_format_is_answer_format() -> None:
    event_time = EventTimeAnswer(event=REF1, start="2026-11-12T01:00:00+00:00")
    assert check_structured(event_time, {"kind": "time_slot", "start": "2026-11-12T10:00:00"}) == (
        "answer_format"
    )
    assert check_structured(event_time, {"kind": "event", "event": "e1"}) == "answer_format"
    assert check_structured(MESSAGES, {"kind": "event_set", "events": [E1, E1]}) == "answer_format"


def test_clarify_expected() -> None:
    assert grade_structured(CLARIFY, {"kind": "clarify"}).verdict == "ok"
    assert grade_structured(CLARIFY, {"kind": "clarify", "x": 1}).verdict == "answer_format"
    first = grade_structured(CLARIFY, {"kind": "event", "event": E1})
    assert (first.verdict, first.matched_reading) == ("ambiguity_ignored", 0)
    second = grade_structured(CLARIFY, {"kind": "event_set", "events": [E2, E1]})
    assert (second.verdict, second.matched_reading) == ("ambiguity_ignored", 1)
    # 어느 해석과도 맞지 않는 유효한 답
    none = grade_structured(CLARIFY, {"kind": "event", "event": E2})
    assert (none.verdict, none.matched_reading) == ("ambiguity_ignored", None)
    assert grade_structured(CLARIFY, {"kind": "abstain"}).verdict == "ambiguity_ignored"
    # 형식 오류는 어느 쪽도 아니다
    assert grade_structured(CLARIFY, {"kind": "event"}).verdict == "answer_format"
    assert grade_structured(CLARIFY, None).verdict == "answer_format"


def test_clarify_on_a_normal_question_is_unneeded() -> None:
    for expected in (
        MESSAGES,
        DOCUMENT,
        ABSTAIN,
        EventAnswer(event=REF1),
        EventSetAnswer(events=[]),
    ):
        grade = grade_structured(expected, {"kind": "clarify"})
        assert (grade.verdict, grade.matched_reading) == ("unneeded_clarify", None)


def test_matched_reading_is_none_unless_ambiguity_ignored() -> None:
    assert grade_structured(MESSAGES, msgs("m1")).matched_reading is None
    assert grade_structured(CLARIFY, {"kind": "clarify"}).matched_reading is None


def test_answer_ids_summarize_new_kinds_safely() -> None:
    assert answer_ids(msgs("m2", "m1")) == {
        "kind": "message_set",
        "messages": [{"message_id": "m2"}, {"message_id": "m1"}],
    }
    assert answer_ids(doc("f1")) == {"kind": "document", "document": {"file_id": "f1"}}
    assert answer_ids({"kind": "abstain"}) == {"kind": "abstain"}
    assert answer_ids({"kind": "clarify"}) == {"kind": "clarify"}


def test_answer_ids_new_kinds_never_copy_free_text() -> None:
    sentence = "10시에 회의가 있습니다. 비밀 문장"
    got = answer_ids(
        {
            "kind": "message_set",
            "messages": [{"message_id": sentence}, "문장", {"message_id": "x" * 65}],
            "document": {"file_id": sentence},
        }
    )
    assert got is not None and "비밀" not in str(got) and "문장" not in str(got)
    assert got["messages"] == [{"message_id": "<invalid>"}] * 3
    assert got["document"] == {"file_id": "<invalid>"}
    got = answer_ids({"kind": "message_set", "messages": sentence, "document": sentence})
    assert got is not None and got["messages"] == "<invalid>"
    assert got["document"] == {"file_id": "<invalid>"}
