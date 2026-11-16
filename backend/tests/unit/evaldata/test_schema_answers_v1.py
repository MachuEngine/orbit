"""평가 스위트 v1 정답 유형(ADR-0026 §4)의 스키마 검증. 데이터는 모두 합성 값이다."""

import pytest
from pydantic import TypeAdapter, ValidationError

from orbit.evaldata.schema import (
    AbstainAnswer,
    ClarifyAnswer,
    DocumentAnswer,
    EventSetAnswer,
    ExpectedAnswer,
    Gold,
    MessageSetAnswer,
    answer_signature,
)

ADAPTER: TypeAdapter[ExpectedAnswer] = TypeAdapter(ExpectedAnswer)
E1 = {"calendar_id": "c1", "event_id": "e1"}
E2 = {"calendar_id": "c1", "event_id": "e2"}


def test_new_kinds_load_through_the_discriminated_union() -> None:
    msg = ADAPTER.validate_python({"kind": "message_set", "messages": [{"message_id": "m1"}]})
    doc = ADAPTER.validate_python({"kind": "document", "document": {"file_id": "f1"}})
    assert isinstance(msg, MessageSetAnswer) and msg.messages[0].message_id == "m1"
    assert isinstance(doc, DocumentAnswer) and doc.document.file_id == "f1"
    assert isinstance(ADAPTER.validate_python({"kind": "abstain"}), AbstainAnswer)
    clarify = ADAPTER.validate_python(
        {
            "kind": "clarify",
            "readings": [{"kind": "event", "event": E1}, {"kind": "event_set", "events": [E1, E2]}],
        }
    )
    assert isinstance(clarify, ClarifyAnswer) and len(clarify.readings) == 2


def test_v0_gold_still_loads() -> None:
    gold = Gold.model_validate({"expected_answer": {"kind": "event_set", "events": [E1]}})
    assert isinstance(gold.expected_answer, EventSetAnswer)


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "abstain", "note": "x"})
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "document", "document": {"file_id": "f1", "x": 1}})


def test_clarify_needs_two_readings() -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "clarify", "readings": [{"kind": "event", "event": E1}]})


def test_clarify_readings_cannot_be_clarify_or_abstain() -> None:
    for bad in ({"kind": "abstain"}, {"kind": "clarify", "readings": []}):
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(
                {"kind": "clarify", "readings": [{"kind": "event", "event": E1}, bad]}
            )


def test_clarify_readings_must_differ_regardless_of_order() -> None:
    same_set = [
        {"kind": "event_set", "events": [E1, E2]},
        {"kind": "event_set", "events": [E2, E1]},
    ]
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "clarify", "readings": same_set})
    # 종류가 다르면 값이 겹쳐도 다른 해석이다
    ok = [{"kind": "event", "event": E1}, {"kind": "event_set", "events": [E1]}]
    assert ADAPTER.validate_python({"kind": "clarify", "readings": ok}) is not None


def test_answer_signature_is_order_free() -> None:
    a = ADAPTER.validate_python(
        {"kind": "message_set", "messages": [{"message_id": "a"}, {"message_id": "b"}]}
    )
    b = ADAPTER.validate_python(
        {"kind": "message_set", "messages": [{"message_id": "b"}, {"message_id": "a"}]}
    )
    assert answer_signature(a) == answer_signature(b)
