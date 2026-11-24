"""rag-v1 본문 검사 규칙: 결정 나열 금지, 마감일 메일 밖의 날짜 기한 금지."""

from __future__ import annotations

import re

import pytest

from orbit.evaldata.rag_v1.build import NO_DEADLINE_PATTERNS, build
from orbit.evaldata.v1.realize_check import check_body
from orbit.evaldata.v1.skeleton import BodySpec, Skeleton

SEED = 7


@pytest.fixture(scope="module")
def sk() -> Skeleton:
    return build(SEED, "test-sha")[0]


def _spec(sk: Skeleton, subject_part: str) -> BodySpec:
    for spec in sk.bodies.values():
        if spec.context.subject and subject_part in spec.context.subject:
            return spec
    raise AssertionError(subject_part)


def _pad(spec: BodySpec, text: str) -> str:
    low = spec.target_chars[0]
    return text + " 추가 설명입니다." * (low // 9 + 1)


def test_decision_mail_rejects_enumerated_decisions(sk: Skeleton) -> None:
    spec = _spec(sk, "결정 사항 공유")
    decision = spec.required_facts[0].surface_forms[0]
    ok = check_body(spec, _pad(spec, f"회의 결과 {decision}로 정했습니다."))
    assert ok.ok, ok.reasons
    for bad in (
        "둘째, 일정 조정",
        "셋째는 비용",
        "몇 가지 중요한 결정이 있습니다",
        f"{decision} 예정",
    ):
        r = check_body(spec, _pad(spec, f"{decision}. {bad}"))
        assert "forbidden_present" in r.reasons, bad


def test_non_deadline_mail_rejects_date_deadline(sk: Skeleton) -> None:
    spec = _spec(sk, "결정 사항 공유")
    assert spec.forbidden_patterns
    for bad in (
        "04월 25일(목)까지 취합하여 공유합니다.",
        "4월 25일까지 부탁드립니다.",
        "10월 3일 자료 제출 기한입니다.",
    ):
        r = check_body(spec, _pad(spec, f"{spec.required_facts[0].surface_forms[0]}. {bad}"))
        assert "forbidden_pattern" in r.reasons, bad
    fine = f"{spec.required_facts[0].surface_forms[0]}. 오후 2시부터 3시 30분까지 회의합니다."
    assert check_body(spec, _pad(spec, fine)).ok


def test_deadline_mail_keeps_date_deadline(sk: Skeleton) -> None:
    spec = _spec(sk, "마감 안내")
    assert not spec.forbidden_patterns
    forms = " ".join(f.surface_forms[0] for f in spec.required_facts)
    date_form = next(f for f in spec.required_facts if f.kind == "deadline").surface_forms[0]
    assert check_body(spec, _pad(spec, f"{forms} {date_form}까지 마감입니다.")).ok


def test_patterns_regex_cases() -> None:
    rx = [re.compile(p) for p in NO_DEADLINE_PATTERNS]
    assert any(r.search("11월 24일(화)까지") for r in rx)
    for bad in (
        "11/24까지",
        "11월 24일 오후 2시까지",
        "11월 24일(월) 오후 2시 30분까지",
        "5월 3일 14시까지",
        "24일까지",
        "11월 24일 전까지",
        "11월 24일 안에",
        "11월 24일(화요일)까지",
        "11월 24일 금요일까지",
        "11/24 14:00까지",
        "11월 24일 이내에 회신 부탁드립니다.",
        "11월 24일 전에 보내 주세요.",
        "11월 27일까지",
        "11월 20일부터 11월 24일까지 제출해 주세요",
        "11월 24일부터 27일까지 휴무입니다.",
        "11/24부터 11/27까지",
    ):
        assert any(r.search(bad) for r in rx), bad
    for ok in (
        "오후 2시부터 3시 30분까지",
        "11월 24일 오후 2시부터 3시 30분까지",
        "2026/11/24 회의",
        "1/2 분량",
        "11월 24일부터 시작",
    ):
        assert not any(r.search(ok) for r in rx), ok


def test_empty_patterns_not_serialized(sk: Skeleton) -> None:
    spec = _spec(sk, "마감 안내")
    assert "forbidden_patterns" not in spec.model_dump(mode="json")
    assert "forbidden_patterns" in _spec(sk, "결정 사항 공유").model_dump(mode="json")
    assert Skeleton.model_validate_json(sk.model_dump_json(by_alias=True)) == sk


def test_decision_mail_forbids_other_decisions(sk: Skeleton) -> None:
    from orbit.evaldata.rag_v1.build import DECISIONS

    spec = _spec(sk, "결정 사항 공유")
    own = spec.required_facts[0].surface_forms[0]
    forms = spec.forbidden_facts[0].surface_forms
    assert own not in forms and all(d in forms for d in DECISIONS if d != own)


def test_invalid_forbidden_pattern_rejected(sk: Skeleton) -> None:
    spec = _spec(sk, "마감 안내")
    with pytest.raises(ValueError, match="정규식"):
        spec.model_validate({**spec.model_dump(), "forbidden_patterns": ["("]})


def test_decisions_replaced_in_place() -> None:
    from orbit.evaldata.rag_v1.build import DECISIONS

    assert len(DECISIONS) == 10 and len(set(DECISIONS)) == 10
    assert DECISIONS[7] == "외부 공유 자료는 PDF로만 배포"
    assert "회의록은 당일 공유" not in DECISIONS


def test_decision_mail_rejects_vague_decision_phrases(sk: Skeleton) -> None:
    spec = _spec(sk, "결정 사항 공유")
    own = spec.required_facts[0].surface_forms[0]
    ok = check_body(spec, _pad(spec, f"회의 결과 {own}하기로 했습니다."))
    assert ok.ok, ok.reasons
    for bad in (
        "첫째, 일정 조정",
        "결정된 사항들을 공유합니다",
        "몇 가지 사항을 정했습니다",
        "결정 내용은 회의록에 정리했습니다",
        "회의록에 결정이 정리되었습니다",
    ):
        r = check_body(spec, _pad(spec, f"{own}. {bad}"))
        assert "forbidden_present" in r.reasons or "forbidden_pattern" in r.reasons, bad
