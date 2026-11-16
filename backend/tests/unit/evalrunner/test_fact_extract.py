"""사실 후보 추출·정규화·근거 대조 (ADR-0029 §2). 데이터는 모두 합성 값이다."""

import json

import pytest

from orbit.evalrunner.fact_extract import (
    Fact,
    build_source_index,
    check_text,
    extract_facts,
    is_grounded,
    source_strings,
)


def keys(text: str, kind: str) -> list[object]:
    return [f.key for f in extract_facts(text) if f.kind == kind]


# ------------------------------------------------------------------ 날짜


@pytest.mark.parametrize(
    "text",
    [
        "11월 16일",
        "2026년 11월 16일",
        "2026-11-16",
        "2026.11.16",
        "2026/11/16",
        "11/16",
        "11월16일",
        "２０２６년 １１월 １６일",  # 전각 숫자
    ],
)
def test_date_notations_normalize_to_month_day(text: str) -> None:
    assert keys(f"일정은 {text}에 있습니다", "date") == [(11, 16)]


def test_date_requires_month_and_day_and_valid_range() -> None:
    assert keys("11월 중에 16일쯤", "date") == []
    assert keys("13월 40일", "date") == []
    assert keys("내일 모레", "date") == []


def test_iso_datetime_gives_date_and_exact_time_without_offset_noise() -> None:
    facts = extract_facts("2026-11-16T14:30:00+09:00")
    assert [f.kind for f in facts] == ["date", "time"]
    assert facts[0].key == (11, 16)
    assert facts[1].key == frozenset({(14, 30)})


# ------------------------------------------------------------------ 시각


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("14:30", {(14, 30)}),
        ("02:30", {(2, 30)}),
        ("오후 2시 30분", {(14, 30)}),
        ("오후 2시30분", {(14, 30)}),
        ("오전 9시", {(9, 0)}),
        ("오전 12시", {(0, 0)}),
        ("오후 12시", {(12, 0)}),
        ("저녁 7시", {(19, 0)}),
        ("2시 반", {(2, 30), (14, 30)}),
        ("9시", {(9, 0), (21, 0)}),
        ("17시", {(17, 0)}),
        ("정오", {(12, 0)}),
        ("자정", {(0, 0)}),
    ],
)
def test_time_notations_normalize_to_24h_possibilities(text: str, expected: set[object]) -> None:
    assert keys(f"회의는 {text}입니다", "time") == [frozenset(expected)]


def test_duration_hours_are_not_times() -> None:
    assert keys("회의는 2시간 동안 진행됩니다", "time") == []
    assert keys("30분 동안", "time") == []


def test_half_hour_marker_requires_word_boundary() -> None:
    assert keys("9시 반복 일정", "time") == [frozenset({(9, 0), (21, 0)})]
    assert keys("9시 반에 시작", "time") == [frozenset({(9, 30), (21, 30)})]


# ------------------------------------------------------------------ 금액·숫자


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("300만원", "3000000"),
        ("30만 원", "300000"),
        ("1,200,000원", "1200000"),
        ("1억 2천만 원", "120000000"),
        ("5,000원", "5000"),
        ("3천원", "3000"),
    ],
)
def test_won_amounts_normalize_to_integers(text: str, value: str) -> None:
    assert keys(f"견적은 {text}입니다", "amount") == [("KRW", value)]


def test_dollar_amounts() -> None:
    assert keys("$1,200 또는 100달러", "amount") == [("USD", "1200"), ("USD", "100")]


def test_amount_without_digits_is_not_a_candidate() -> None:
    assert keys("만원 정도", "amount") == []


def test_numbers_skip_counts_durations_list_markers_and_handles() -> None:
    text = "1. 첫째 항목\n2. 둘째 항목\n일정 3개, 참석 4명, 30분, 2시간, 7번째, E3, M12, 총 150 건"
    assert keys(text, "number") == []  # 마지막 '150 건'도 개수 단위(공백 허용)
    assert keys("재고는 1,250이고 비율은 3.50입니다", "number") == ["1250", "3.5"]


def test_numbers_exclude_spans_taken_by_dates_times_amounts() -> None:
    text = "11월 16일 오후 2시 300만원 그리고 77"
    assert keys(text, "number") == ["77"]


def test_very_long_digit_string_does_not_hang() -> None:
    assert keys("9" * 4000, "number") == ["9" * 4000]


# ------------------------------------------------------------------ 이메일·이름


def test_email_is_lowercased_and_not_double_counted_as_number() -> None:
    facts = extract_facts("문의는 Dain.Bae2@Hanbitlab.example.org 로 주세요")
    assert [(f.kind, f.key) for f in facts] == [("email", "dain.bae2@hanbitlab.example.org")]


def test_name_needs_surname_three_syllables_and_title() -> None:
    assert keys("배다인 님이 보낸 메일과 송도윤 팀장님의 일정", "name") == ["배다인", "송도윤"]
    assert keys("고객사 담당자와 마감일 정리", "name") == []  # 호칭이 없거나 성씨가 아니다
    assert keys("다인 님", "name") == []  # 2글자 이름은 뽑지 못한다(문서화한 한계)


# ------------------------------------------------------------------ 근거 대조


TOOL_RESULT = json.dumps(
    {
        "events": [
            {
                "handle": "E1",
                "title": "분기 계획 점검",
                "start": "2026-11-16T14:30:00+09:00",
                "attendees": [{"name": "윤서진", "email": "seojin.yoon@example.org"}],
            }
        ],
        "total": 3,
        "body": "견적은 300만원입니다. 마감은 12월 3일 오전 10시입니다.",
    },
    ensure_ascii=False,
)


def grounded(answer: str, extra: list[str] | None = None) -> dict[str, bool]:
    sources = source_strings(TOOL_RESULT) + (extra or [])
    return {f.text: ok for f, ok in check_text(answer, sources)}


def test_grounded_when_tool_result_has_the_same_normalized_value() -> None:
    result = grounded(
        "11월 16일 오후 2시 30분 분기 계획 점검이 있고 견적은 3,000,000원, 마감은 12/3 10시"
    )
    assert result == {
        "11월 16일": True,
        "오후 2시 30분": True,
        "3,000,000원": True,
        "12/3": True,
        "10시": True,  # 오전·오후 표시가 없으면 가능 값이 겹치면 근거 있음
    }


def test_ungrounded_values_are_flagged() -> None:
    result = grounded(
        "11월 17일 오후 4시에 윤서진 님과 만나고 비용은 500만원입니다. 연락은 x@y.org"
    )
    assert result["11월 17일"] is False
    assert result["오후 4시"] is False
    assert result["500만원"] is False
    assert result["x@y.org"] is False
    assert result["윤서진"] is True


def test_morning_afternoon_mismatch_is_ungrounded() -> None:
    assert grounded("오전 2시 30분")["오전 2시 30분"] is False
    assert grounded("오후 2시 30분")["오후 2시 30분"] is True


def test_user_utterance_counts_as_source() -> None:
    assert grounded("12월 25일 일정은 없습니다")["12월 25일"] is False
    assert grounded("12월 25일 일정은 없습니다", ["12월 25일에 뭐 있어?"])["12월 25일"] is True


def test_number_grounded_by_any_digit_run_in_sources() -> None:
    assert grounded("총 3 가지입니다") == {}  # 개수 단위(가지)는 후보에서 빠진다
    result = grounded("코드는 3과 99입니다")
    assert result == {"3": True, "99": False}


def test_source_strings_skip_keys_and_non_json_is_raw() -> None:
    assert source_strings('{"a": "x", "b": [1, true, null, {"c": "y"}]}') == ["x", "1", "y"]
    assert source_strings("그냥 텍스트") == ["그냥 텍스트"]


def test_is_grounded_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="알 수 없는"):
        is_grounded(Fact("color", "red", "red"), build_source_index([]))
