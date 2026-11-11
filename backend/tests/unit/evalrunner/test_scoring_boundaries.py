"""숫자 경계 회귀 테스트: 일치 위치 바로 앞·뒤 글자가 숫자면 일치가 아니다."""

import pytest

from orbit.evaldata.schema import AnswerFact, Gold
from orbit.evalrunner.scoring import check_answer_facts, check_must_not_contain, contains


def _gold(candidate: str) -> Gold:
    return Gold(
        answer_facts=[AnswerFact(fact_id="f", any_of=[candidate], description="")],
        must_not_contain=[candidate],
    )


CASES = [
    # (후보, 응답, 일치해야 하는가)
    ("2시", "12시에 만납니다", False),
    ("2시", "오후 2시에 만납니다", True),
    ("11월 1일", "11월 12일입니다", False),
    ("11월 12일", "11월 12일입니다", True),
    ("3시", "13시입니다", False),
    ("3시", "3시입니다", True),
    ("14:00", "114:00입니다", False),
    ("14:00", "14:00입니다", True),
    ("10시", "10시 30분입니다", False),
    ("오전 10시", "오전 10시 30분입니다", False),
    ("10시", "10시에 회의가 있습니다", True),
    ("오전 10시", "오전 10시에 회의가 있습니다", True),
    ("11월 16일", "11월 16일 17시입니다", True),
    ("11월 16일", "11월16일17시입니다", True),
    ("10시", "10시30분입니다", False),
    ("10시", "10시 반에 만납니다", False),
    ("10시", "10시 반부터 가능합니다", False),
    ("10시", "10시 반.", False),
    ("9시", "9시 반복 회의입니다", True),
    ("9시", "9시 반이 좋습니다", False),
    ("10시", "10시간 걸립니다", False),
    ("10시", "10시 간식 시간입니다", True),
    ("10시", "10시반부터입니다", False),
    ("9시", "9시반복 회의입니다", True),
    ("2시", "11월 16일 2시입니다", True),
    ("2시", "12시와 2시입니다", True),
    ("오후5시", "오후 5시입니다", True),
    ("오후 5시", "오후5시입니다", True),
    ("11/16", "11/16 17:00입니다", True),
    ("2026-11-16", "2026-11-16 17:00입니다", True),
    ("17:00", "11/16 17:00입니다", True),
    ("14:00", "14:001입니다", False),
]


@pytest.mark.parametrize(("candidate", "response", "expected"), CASES)
def test_number_boundary_in_answer_facts_and_must_not_contain(
    candidate: str, response: str, expected: bool
) -> None:
    gold = _gold(candidate)
    assert contains(response, candidate) is expected
    assert check_answer_facts(gold, response) is expected
    assert check_must_not_contain(gold, response) is (not expected)


def test_whitespace_is_flexible_between_characters_but_not_between_digits() -> None:
    assert contains("회의는   오후\t2 시", "오후 2시")
    assert not contains("1 2시", "12시")  # 후보 안 숫자 사이에는 공백을 허용하지 않는다


def test_second_occurrence_is_checked_after_a_rejected_one() -> None:
    assert contains("12시와 2시", "2시")  # 첫 일치(12시 안)는 거부, 두 번째는 통과


def test_leading_digit_rejection_applies_only_to_digit_initial_candidates() -> None:
    assert contains("09:00베타 고객 미팅", "베타 고객 미팅")
    assert not contains("12시", "2시")
