"""본문 결함 검사 테스트 (평가 v1 ③). 모든 본문은 합성 문장이다."""

from orbit.evaldata.v1.body_checks import (
    confirmed_other_value,
    foreign_years,
    topic_body_defect,
    unfirm_answer,
)

AMOUNT = ["9,000,000원", "900만원"]
DATE = ["11월 28일", "11/28", "11월 28일(토)"]


def test_foreign_years_flags_other_years_but_not_world_year_or_ids() -> None:
    assert foreign_years("배송일은 2024년 5월 15일입니다.", 2026) == ["2024"]
    assert foreign_years("결제 기한은 2024-07-31입니다. 2027년 1월 계약", 2026) == ["2024", "2027"]
    assert foreign_years("2026년 10월 23일에 보냈고 2026-11-01에 갱신", 2026) == []
    # 문서 번호와 금액은 연도 표기가 아니다
    assert foreign_years("문서 번호: 20241023-001, 합계 12,024원, 2024개", 2026) == []


def test_confirmed_other_value_needs_a_confirm_word_and_a_different_value() -> None:
    done = "정산 금액 5,400,000원으로 정산이 완료되었습니다."
    assert confirmed_other_value(done, "amount", AMOUNT)
    assert confirmed_other_value("11월 23일에 확정되었습니다.", "deadline", DATE)
    # 정답과 같은 값이면 충돌이 아니다
    assert confirmed_other_value("11월 28일로 확정되었습니다.", "deadline", DATE) is None
    # 확정 표현이 없는 문장의 다른 값은 교란 문서의 정상 모습이다
    assert confirmed_other_value("당시 승인 기한은 11월 20일이었습니다.", "deadline", DATE) is None
    assert confirmed_other_value("확정되었습니다.", "unknown_kind", DATE) is None


def test_unfirm_answer_flags_estimates_near_the_value_and_placeholders() -> None:
    assert unfirm_answer("총 900만원이 소요될 것으로 예상됩니다.", AMOUNT) is not None
    assert unfirm_answer("잠정 900만원입니다.", AMOUNT) is not None
    assert unfirm_answer("총정산액은 000원입니다. 900만원", AMOUNT) == "금액이 자리표시자"
    far = "예상 일정은 따로 공유합니다." + "가" * 40 + "금액은 900만원입니다."
    assert unfirm_answer(far, AMOUNT) is None
    assert unfirm_answer("총 900만원입니다. 1,000원 별도", AMOUNT) is None


def test_topic_body_defect_reports_the_first_reason_and_ignores_grade_zero() -> None:
    clean = {"a": (2, "금액은 900만원입니다."), "b": (1, "이전 금액은 800만원이었습니다.")}
    assert topic_body_defect(clean, "amount", AMOUNT, 2026) is None
    assert (
        topic_body_defect({**clean, "c": (1, "2024년 7월 안내")}, "amount", AMOUNT, 2026)
        == "foreign_year"
    )
    assert topic_body_defect({**clean, "c": (0, "2024년 7월 안내")}, "amount", AMOUNT, 2026) is None
    assert (
        topic_body_defect(
            {"a": (2, "900만원"), "b": (1, "5,400,000원으로 정산 완료")}, "amount", AMOUNT, 2026
        )
        == "grade1_confirmed_other_value"
    )
    assert (
        topic_body_defect(
            {"a": (2, "총 900만원 예상"), "b": (1, "이전 800만원")}, "amount", AMOUNT, 2026
        )
        == "grade2_estimate"
    )
    assert (
        topic_body_defect(
            {"a": (2, "합계 000원, 900만원"), "b": (1, "이전 800만원")}, "amount", AMOUNT, 2026
        )
        == "grade2_placeholder"
    )
