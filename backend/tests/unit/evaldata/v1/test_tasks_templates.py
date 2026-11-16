"""종단 과제 질문 템플릿·날짜 표현 테스트 (ADR-0026 §4). 데이터는 모두 합성 값이다."""

import random
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orbit.evaldata.v1 import task_templates as tt
from orbit.evalrunner.scoring import contains

LA = ZoneInfo("America/Los_Angeles")
SEOUL = ZoneInfo("Asia/Seoul")
AS_OF_DAYS = [date(2026, 11, 16) + timedelta(days=i) for i in range(0, 10)]  # 월요일부터


@pytest.mark.parametrize("as_of", AS_OF_DAYS)
def test_relative_phrases_round_trip_for_every_nearby_day(as_of: date) -> None:
    for offset in range(-25, 26):
        target = as_of + timedelta(days=offset)
        for phrase in tt.relative_date_phrases(target, as_of):
            assert tt.resolve_date_phrase(phrase, as_of) == target, (as_of, target, phrase)
        explicit = tt.explicit_date(target)
        assert tt.resolve_date_phrase(explicit, as_of) == target


def test_known_relative_phrases() -> None:
    as_of = date(2026, 11, 18)  # 수요일
    assert tt.relative_date_phrases(date(2026, 11, 19), as_of) == ["내일"]
    assert tt.relative_date_phrases(date(2026, 11, 18), as_of) == ["오늘"]
    assert tt.relative_date_phrases(date(2026, 11, 17), as_of) == ["어제"]
    assert tt.relative_date_phrases(date(2026, 11, 24), as_of) == ["다음 주 화요일"]
    assert tt.relative_date_phrases(date(2026, 11, 11), as_of) == ["지난주 수요일"]
    assert tt.relative_date_phrases(date(2026, 11, 20), date(2026, 11, 16)) == ["이번 주 금요일"]
    assert tt.relative_date_phrases(date(2026, 11, 20), as_of) == ["모레"]
    assert tt.relative_date_phrases(date(2026, 12, 25), as_of) == []  # 멀면 절대 날짜만 쓴다


def test_phrase_choice_falls_back_to_explicit_date() -> None:
    rng = random.Random(1)  # noqa: S311  재현 가능한 표본 선택
    far = date(2026, 12, 25)
    assert tt.pick_date_phrase(rng, far, date(2026, 11, 18), relative=True) == "12월 25일"
    assert tt.pick_date_phrase(rng, date(2026, 11, 19), date(2026, 11, 18), relative=False) == (
        "11월 19일"
    )


def test_explicit_date_resolves_across_year_boundary() -> None:
    assert tt.resolve_date_phrase("1월 2일", date(2026, 12, 30)) == date(2027, 1, 2)
    assert tt.resolve_date_phrase("12월 30일", date(2027, 1, 3)) == date(2026, 12, 30)
    assert tt.resolve_date_phrase("2월 30일", date(2026, 2, 1)) is None
    assert tt.resolve_date_phrase("아무 때나", date(2026, 2, 1)) is None


@pytest.mark.parametrize("as_of", AS_OF_DAYS)
def test_week_phrases_round_trip(as_of: date) -> None:
    for weeks in range(-6, 7):
        monday = tt.week_start(as_of) + timedelta(days=7 * weeks)
        phrase = tt.week_phrase(monday, as_of)
        assert tt.resolve_week_phrase(phrase, as_of) == monday, phrase


def test_time_forms_use_the_user_time_zone() -> None:
    moment = datetime(2026, 10, 27, 16, 30, tzinfo=UTC)  # LA 현지 09:30(일광 절약 시간)
    forms = tt.local_time_forms(moment, LA)
    assert "09:30" in forms and "오전 9시 30분" in forms
    assert "01:30" in tt.local_time_forms(moment, SEOUL)  # 서울은 다음 날 01:30


def test_whole_hour_forms_do_not_match_half_hour_text() -> None:
    forms = tt.local_time_forms(datetime(2026, 11, 5, 5, 0, tzinfo=UTC), SEOUL)  # 14:00
    assert any(contains("오후 2시에 시작합니다", f) for f in forms)
    assert not any(contains("오후 2시 30분에 시작합니다", f) for f in forms)
    assert not any(contains("14:30", f) for f in forms)


def test_duration_and_clock_text() -> None:
    assert tt.korean_duration(30) == "30분"
    assert tt.korean_duration(60) == "1시간"
    assert tt.korean_duration(90) == "1시간 30분"
    moment = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
    assert tt.korean_clock(moment, SEOUL) == "오전 9시"
    assert tt.korean_clock(moment + timedelta(hours=9, minutes=30), SEOUL) == "오후 6시 30분"


def _all_templates() -> list[str]:
    out: list[str] = []
    for value in vars(tt).values():
        if isinstance(value, tuple) and value and all(isinstance(v, str) for v in value):
            out += list(value)
        if isinstance(value, dict):
            for v in value.values():
                if isinstance(v, tuple) and all(isinstance(x, str) for x in v):
                    out += list(v)
    return out


def test_templates_have_two_to_four_wordings_each() -> None:
    groups = [
        v
        for v in vars(tt).values()
        if isinstance(v, tuple) and v and all(isinstance(x, str) for x in v)
    ]
    groups += [
        w
        for d in vars(tt).values()
        if isinstance(d, dict)
        for w in d.values()
        if isinstance(w, tuple)
    ]
    assert groups
    assert all(2 <= len(g) <= 4 for g in groups)


def test_templates_carry_no_provider_ids() -> None:
    pattern = re.compile(r"ev\d{5,}|msg-\d|doc-\d|cal-u|acct-|thr-")
    assert not [t for t in _all_templates() if pattern.search(t)]
