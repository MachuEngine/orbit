"""사실(Fact) 만들기와 문자열 대조. 시각 표기는 사용자 현지 시간대 기준이다."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from orbit.evaldata.v1.skeleton import Fact, FactKind

WEEKDAYS_KO = "월화수목금토일"
_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """대조용 정규화: NFC, 소문자, 모든 공백 제거."""
    return _WS.sub("", unicodedata.normalize("NFC", text)).casefold()


def fact_in(text: str, fact: Fact) -> bool:
    """본문에 사실의 표기 중 하나라도 들어 있는가."""
    body = norm(text)
    return any(norm(form) in body for form in fact.surface_forms)


def conflicts(a: Fact, b: Fact) -> bool:
    """한 사실의 표기가 다른 사실의 표기에 부분 문자열로 들어 있으면 True.

    예: 필수 "오후 2시"와 금지 "오후 2시 30분"은 필수 쪽 표기가 금지 표기의 일부라 본문에서 구분되지
    않는다. 생성기는 이런 쌍을 만들지 않는다.
    """
    for x in a.surface_forms:
        for y in b.surface_forms:
            nx, ny = norm(x), norm(y)
            if nx in ny or ny in nx:
                return True
    return False


def date_forms(day: date) -> list[str]:
    wk = WEEKDAYS_KO[day.weekday()]
    return [
        f"{day.month}월 {day.day}일",
        f"{day.month}/{day.day}",
        f"{day.month}월 {day.day}일({wk})",
    ]


def time_forms(moment: datetime) -> list[str]:
    h, m = moment.hour, moment.minute
    ampm = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    if m == 0:
        return [f"{ampm} {h12}시", f"{h:02d}:00"]
    if m == 30:
        return [f"{ampm} {h12}시 30분", f"{h:02d}:30"]
    return [f"{ampm} {h12}시 {m}분", f"{h:02d}:{m:02d}"]


def datetime_forms(moment: datetime) -> list[str]:
    d, t = date_forms(moment.date()), time_forms(moment)
    return [f"{d[0]} {t[0]}", f"{d[1]} {t[1]}"]


def date_fact(fact_id: str, day: date, kind: FactKind = "date") -> Fact:
    return Fact(fact_id=fact_id, kind=kind, value=day.isoformat(), surface_forms=date_forms(day))


def time_fact(fact_id: str, moment: datetime) -> Fact:
    return Fact(
        fact_id=fact_id, kind="time", value=moment.isoformat(), surface_forms=time_forms(moment)
    )


def datetime_fact(fact_id: str, moment: datetime, kind: FactKind = "datetime") -> Fact:
    """`moment`는 사용자 현지 시각(aware)이다. 값은 현지 오프셋이 붙은 ISO 8601이다."""
    return Fact(
        fact_id=fact_id, kind=kind, value=moment.isoformat(), surface_forms=datetime_forms(moment)
    )


def amount_fact(fact_id: str, won: int) -> Fact:
    forms = [f"{won:,}원"]
    if won % 10_000 == 0:
        forms.append(f"{won // 10_000:,}만원")
    return Fact(fact_id=fact_id, kind="amount", value=won, surface_forms=forms)


def text_fact(fact_id: str, kind: FactKind, value: str, forms: list[str]) -> Fact:
    return Fact(fact_id=fact_id, kind=kind, value=value, surface_forms=forms)


def person_fact(fact_id: str, name: str) -> Fact:
    return Fact(fact_id=fact_id, kind="person", value=name, surface_forms=[name])
