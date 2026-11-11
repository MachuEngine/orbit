"""숫자 경계 회귀 테스트: 일치 위치 바로 앞·뒤 글자가 숫자면 일치가 아니다."""

import pytest

from orbit.evalrunner.scoring import check_answer_facts, check_must_not_contain, contains
from orbit.evalrunner.task_source import FactRecord, GoldRecord


def _gold(candidate: str) -> GoldRecord:
    return GoldRecord(
        answer_facts=(FactRecord("f", (candidate,), ""),),
        must_not_contain=(candidate,),
        expected_changes=None,
        required_tool_calls=(),
        forbidden_tools=(),
    )


CASES = [
    # (후보, 응답, 일치해야 하는가)
    ("2시", "12시에 만납니다", False),
    ("2시", "오후 2시에 만납니다", True),
    ("12시", "1 2시에 만납니다", False),  # 공백 제거로 숫자가 붙으면 안 된다
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
]


@pytest.mark.parametrize(("candidate", "response", "expected"), CASES)
def test_number_boundary_in_answer_facts_and_must_not_contain(
    candidate: str, response: str, expected: bool
) -> None:
    gold = _gold(candidate)
    assert contains(response, candidate) is expected
    assert check_answer_facts(gold, response) is expected
    assert check_must_not_contain(gold, response) is (not expected)


def test_digit_separating_whitespace_is_kept_but_other_whitespace_is_dropped() -> None:
    assert contains("1 2시", "1 2시")  # 같은 표기는 일치
    assert not contains("1 2시", "12시")
    assert contains("회의는   오후\t2 시", "오후 2시")


def test_second_occurrence_is_checked_after_a_rejected_one() -> None:
    assert contains("12시와 2시", "2시")  # 첫 일치(12시 안)는 거부, 두 번째는 통과
