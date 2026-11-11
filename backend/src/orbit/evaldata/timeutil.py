"""시각·날짜 계산과 정답 표현(surface form) 생성.

v0 한계: 두 가상 사용자는 모두 Asia/Seoul(고정 +09:00, 일광절약시간 없음)이고, 모든 일정은
평일 09:00~18:00(KST)에 있어 UTC 날짜와 KST 날짜가 같다. Sim의 날짜 처리는 UTC 기준이므로
종일 일정은 만들지 않는다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")
WEEKDAYS_KO = "월화수목금토일"


def at_local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


def local_date(moment: datetime) -> date:
    return moment.astimezone(KST).date()


def to_utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def week_start(day: date) -> date:
    """그 날이 속한 주의 월요일."""
    return day - timedelta(days=day.weekday())


def weekday_ko(day: date) -> str:
    return WEEKDAYS_KO[day.weekday()]


def half_of(moment: datetime) -> str:
    return "오전" if moment.astimezone(KST).hour < 12 else "오후"


def kor_time(moment: datetime) -> str:
    """'오후 2시', '오전 9시 30분'."""
    local = moment.astimezone(KST)
    ampm = "오전" if local.hour < 12 else "오후"
    h12 = local.hour % 12 or 12
    if local.minute == 0:
        return f"{ampm} {h12}시"
    return f"{ampm} {h12}시 {local.minute}분"


def kor_date(day: date) -> str:
    """'11월 17일(화)'."""
    return f"{day.month}월 {day.day}일({weekday_ko(day)})"


def kor_when(moment: datetime) -> str:
    local = moment.astimezone(KST)
    return f"{kor_date(local.date())} {kor_time(local)}"


def hhmm(moment: datetime) -> str:
    local = moment.astimezone(KST)
    return f"{local.hour:02d}:{local.minute:02d}"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def time_forms(moment: datetime) -> list[str]:
    """시각의 표면형. 검사기는 공백을 무시하고 부분 문자열로 비교한다.

    분이 0인 경우 '14시'는 '14시 30분'에도 들어 있으므로 느슨한 표현이다(v0 한계).
    """
    local = moment.astimezone(KST)
    h, m = local.hour, local.minute
    ampm = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    forms = [f"{h:02d}:{m:02d}", f"{h}:{m:02d}", f"{ampm} {h12}:{m:02d}"]
    if m == 0:
        forms += [f"{ampm} {h12}시", f"{h}시"]
        if h == 12:
            forms.append("정오")
    elif m == 30:
        forms += [f"{ampm} {h12}시 30분", f"{ampm} {h12}시반", f"{h}시 30분", f"{h}시반"]
    else:
        forms += [f"{ampm} {h12}시 {m}분", f"{h}시 {m}분"]
    return _dedupe(forms)


def date_forms(day: date) -> list[str]:
    forms = [
        f"{day.month}월 {day.day}일",
        f"{day.month:02d}월 {day.day:02d}일",
        f"{day.month}/{day.day}",
        f"{day.month:02d}/{day.day:02d}",
        day.isoformat(),
    ]
    return _dedupe(forms)
