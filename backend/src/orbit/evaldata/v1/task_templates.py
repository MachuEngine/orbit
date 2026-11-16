"""종단 과제 질문 문구 템플릿과 날짜 표현 (ADR-0026 §4).

- 템플릿마다 2~4가지 표현을 두고 시드로 고른다. 질문 문장만 보고 답 종류가 하나로 정해지게 쓴다
  (목록이면 "전부", 하나면 "다음 하나", 시각이면 "몇 시"). 제공자 ID는 질문에 넣지 않는다.
- 날짜는 as_of 날짜와 사용자 시간대로 계산한 상대 표현("내일", "다음 주 화요일") 또는 "M월 D일"이다.
  `resolve_date_phrase`는 표현을 날짜로 되돌려 계산이 맞는지 확인하는 데 쓴다.
- 시각·날짜의 정답 표기(`local_time_forms`, `local_date_forms`)는 사용자 현지 시간대 기준이다.
"""

from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS_KO = "월화수목금토일"

# ---------------------------------------------------------------- 날짜 표현


def week_start(day: date) -> date:
    """그 날이 속한 주(월요일 시작)의 월요일."""
    return day - timedelta(days=day.weekday())


def weekday_ko(day: date) -> str:
    return WEEKDAYS_KO[day.weekday()]


def explicit_date(day: date) -> str:
    return f"{day.month}월 {day.day}일"


def relative_date_phrases(target: date, as_of_day: date) -> list[str]:
    """`target`을 as_of 날짜 기준으로 부를 수 있는 상대 표현. 없으면 빈 목록."""
    delta = (target - as_of_day).days
    out: list[str] = []
    named = {-1: "어제", 0: "오늘", 1: "내일", 2: "모레"}
    if delta in named:
        out.append(named[delta])
    weeks = (week_start(target) - week_start(as_of_day)).days // 7
    wk = weekday_ko(target)
    if weeks == 0 and delta not in named:
        out.append(f"이번 주 {wk}요일")
    elif weeks == 1:
        out.append(f"다음 주 {wk}요일")
    elif weeks == -1:
        out.append(f"지난주 {wk}요일")
    return out


def pick_date_phrase(rng: random.Random, target: date, as_of_day: date, *, relative: bool) -> str:
    """상대 표현이 있으면(`relative`일 때) 그것을, 없으면 "M월 D일"을 돌려준다."""
    options = relative_date_phrases(target, as_of_day) if relative else []
    if options:
        return rng.choice(options)
    return explicit_date(target)


_WEEK_NAMES = {0: "이번 주", 1: "다음 주", -1: "지난주", 2: "다다음 주", -2: "지지난주"}
_WEEK_EXPLICIT = re.compile(r"(\d{1,2})월 (\d{1,2})일부터 시작하는 주")


def week_phrase(week_monday: date, as_of_day: date) -> str:
    """월요일 `week_monday`로 시작하는 주를 as_of 기준으로 부르는 표현."""
    offset = (week_monday - week_start(as_of_day)).days // 7
    return _WEEK_NAMES.get(offset) or f"{explicit_date(week_monday)}부터 시작하는 주"


def resolve_week_phrase(phrase: str, as_of_day: date) -> date | None:
    """`week_phrase`의 역함수(그 주의 월요일). 해석하지 못하면 None."""
    for offset, name in _WEEK_NAMES.items():
        if phrase == name:
            return week_start(as_of_day) + timedelta(days=7 * offset)
    m = _WEEK_EXPLICIT.fullmatch(phrase)
    if m:
        return resolve_date_phrase(f"{m.group(1)}월 {m.group(2)}일", as_of_day)
    return None


_EXPLICIT = re.compile(r"(\d{1,2})월 (\d{1,2})일")
_WEEK = re.compile(r"(이번 주|다음 주|지난주) ([월화수목금토일])요일")
_NAMED = {"어제": -1, "오늘": 0, "내일": 1, "모레": 2}


def resolve_date_phrase(phrase: str, as_of_day: date) -> date | None:
    """`pick_date_phrase`의 역함수. 해석하지 못하면 None. 연도는 as_of에 가장 가까운 해다."""
    if phrase in _NAMED:
        return as_of_day + timedelta(days=_NAMED[phrase])
    m = _WEEK.fullmatch(phrase)
    if m:
        offset = {"이번 주": 0, "다음 주": 1, "지난주": -1}[m.group(1)]
        base = week_start(as_of_day) + timedelta(days=7 * offset)
        return base + timedelta(days=WEEKDAYS_KO.index(m.group(2)))
    m = _EXPLICIT.fullmatch(phrase)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        candidates = []
        for year in (as_of_day.year - 1, as_of_day.year, as_of_day.year + 1):
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                continue
        return min(candidates, key=lambda d: abs((d - as_of_day).days)) if candidates else None
    return None


# ---------------------------------------------------------------- 정답 표기 (사용자 현지 기준)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def local_time_forms(moment: datetime, tz: ZoneInfo) -> list[str]:
    """시각의 표기들. 채점은 공백을 무시하고 "10시"가 "10시 30분"에 일치하지 않게 본다."""
    local = moment.astimezone(tz)
    h, m = local.hour, local.minute
    ampm = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    forms = [f"{h:02d}:{m:02d}", f"{h}:{m:02d}", f"{ampm} {h12}:{m:02d}"]
    if m == 0:
        forms += [f"{ampm} {h12}시", f"{h}시"]
    elif m == 30:
        forms += [f"{ampm} {h12}시 30분", f"{ampm} {h12}시반", f"{h}시 30분", f"{h}시반"]
    else:
        forms += [f"{ampm} {h12}시 {m}분", f"{h}시 {m}분"]
    return _dedupe(forms)


def local_date_forms(day: date) -> list[str]:
    return _dedupe(
        [
            f"{day.month}월 {day.day}일",
            f"{day.month}/{day.day}",
            f"{day.month:02d}/{day.day:02d}",
            day.isoformat(),
        ]
    )


def korean_duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours}시간 {rest}분"
    return f"{hours}시간" if hours else f"{rest}분"


def korean_clock(moment: datetime, tz: ZoneInfo) -> str:
    """'오전 9시', '오후 2시 30분'."""
    local = moment.astimezone(tz)
    ampm = "오전" if local.hour < 12 else "오후"
    h12 = local.hour % 12 or 12
    return f"{ampm} {h12}시" + (f" {local.minute}분" if local.minute else "")


# ---------------------------------------------------------------- 질문 템플릿
# `{when}`은 날짜 표현, 그 밖의 필드는 각 빌더가 채운다. 템플릿마다 의미가 같은 표현 2~4개다.

DAY_AGENDA = (
    "{when} 일정 알려줘",
    "{when} 잡힌 일정 전부 알려줘",
    "{when} 내 일정 전체가 어떻게 돼?",
    "{when} 일정을 빠짐없이 알려줘",
)
CANCELLATION = (
    "{when} 일정 전부 알려줘. 취소된 일정은 빼고.",
    "{when} 일정 중에서 취소된 건 제외하고 전부 알려줘",
    "{when} 취소되지 않은 일정만 전부 알려줘",
)
ALL_DAY = (
    "{when} 일정 전부 알려줘. 종일 일정도 포함해서.",
    "{when} 하루 종일 일정까지 포함해서 일정 전체를 알려줘",
    "종일 일정을 포함해서 {when} 일정을 다 알려줘",
)
FREE_SLOT_FUTURE = (
    "{when} 근무 시간({work}) 중에 {dur} 이상 비어 있는 시간 가운데 가장 이른 시작 시각을 알려줘",
    "{when} {work} 사이에 {dur} 이상 이어서 비는 가장 빠른 시간이 몇 시부터야?",
    "{when} {dur} 이상 비는 시간 중 제일 빠른 시작 시각이 언제야? 근무 시간은 {work}로 봐줘",
)
FREE_SLOT_PAST = (
    "{when} 근무 시간({work}) 중에 {dur} 이상 비어 있던 시간 가운데 가장 이른 시작 시각을 알려줘",
    "{when} {work} 사이에 {dur} 이상 이어서 비었던 가장 빠른 시간이 몇 시부터였어?",
    "{when} {dur} 이상 비었던 시간 중 제일 빠른 시작 시각이 언제였어? 근무 시간은 {work}로 봐줘",
)
RESCHEDULE = (
    "원래 {old}에 하기로 했던 ‘{title}’ 일정이 바뀌었다던데, 지금은 언제로 잡혀 있어?",
    "{old}로 잡혀 있던 ‘{title}’ 일정 시간이 변경됐다고 해서 그러는데, 현재 몇 시로 잡혀 있어?",
    "‘{title}’ 일정(원래 {old})이 옮겨졌다는데 지금 시작 시각이 어떻게 돼?",
)
PROJECT_WEEK = (
    "{week} {project} 프로젝트 일정을 전부 알려줘",
    "{week} {project} 프로젝트 관련 일정이 뭐뭐 있어? 전부 알려줘",
    "{week}에 잡힌 {project} 프로젝트 일정 전체를 보여줘",
)
PERSON_NEXT = (
    "{name}님과의 다음 미팅이 언제야?",
    "{name}님과 다음에 잡힌 미팅은 언제야?",
    "{name}님과 가장 가까운 다음 미팅 하나만 알려줘",
)
CROSS_MIDNIGHT = (
    "{date}에 시작해서 다음 날까지 이어지는 일정이 뭐야?",
    "{date}에 시작해서 자정을 넘기는 일정이 어떤 거야?",
)
TZ_EVENT = (
    "{date} ‘{title}’ 일정은 내 시간대 기준으로 몇 시에 시작해?",
    "{date}에 있는 ‘{title}’ 일정의 시작 시각이 내 시간대로 언제야?",
)
MISMATCH = (
    "{date} ‘{title}’ 일정은 몇 시에 시작해?",
    "{date}에 있는 ‘{title}’ 일정 시작 시각이 언제로 잡혀 있어?",
    "{date} ‘{title}’ 일정이 몇 시로 잡혀 있는지 알려줘",
)

MAIL_FACT = {
    "deadline": (
        "{project} 프로젝트 {item} 제출 마감일이 언제로 안내됐어? 그 내용이 담긴 가장 최근 메일도 "
        "찾아줘",
        "{project} 프로젝트 {item} 마감일을 가장 최근에 안내한 메일을 찾아서, 마감일이 언제인지 "
        "알려줘",
    ),
    "amount": (
        "{project} 프로젝트 {item} 견적 금액이 얼마로 안내됐어? 그 내용이 담긴 가장 최근 메일도 "
        "찾아줘",
        "{project} 프로젝트 {item} 견적 금액을 가장 최근에 안내한 메일을 찾아서, 금액이 얼마인지 "
        "알려줘",
    ),
    "place": (
        "{project} 프로젝트 {item} 논의 장소가 어디로 안내됐어? 그 내용이 담긴 가장 최근 메일도 "
        "찾아줘",
        "{project} 프로젝트 {item} 논의 장소를 가장 최근에 안내한 메일을 찾아서, 장소가 어디인지 "
        "알려줘",
    ),
    "decision": (
        "{project} 프로젝트 {item} 건에서 최종으로 결정된 사항이 뭐야? 그 내용이 담긴 가장 최근 "
        "메일도 찾아줘",
        "{project} 프로젝트 {item} 결정 사항을 가장 최근에 안내한 메일을 찾아서, 무엇으로 "
        "결정됐는지 알려줘",
    ),
    "attachment": (
        "{project} 프로젝트 {item} 최신 첨부 파일 이름이 뭐야? 그 파일이 첨부된 가장 최근 메일도 "
        "찾아줘",
        "{project} 프로젝트 {item}의 가장 최근 첨부 파일이 붙은 메일을 찾아서, 파일 이름을 "
        "알려줘",
    ),
}

DOC_FACT = {
    "decision": (
        "‘{title}’ 문서에는 어떤 사항이 결정됐다고 적혀 있어? 어느 문서인지도 같이 알려줘",
        "‘{title}’ 문서에 적힌 결정 사항이 뭐야? 해당 문서도 함께 알려줘",
    ),
    "amount": (
        "‘{title}’ 문서에 적힌 견적 금액이 얼마야? 어느 문서인지도 같이 알려줘",
        "‘{title}’ 문서의 견적 금액을 알려주고, 해당 문서도 함께 알려줘",
    ),
    "date": (
        "‘{title}’ 문서에 적힌 일정이 언제야? 어느 문서인지도 같이 알려줘",
        "‘{title}’ 문서에 적힌 날짜가 언제야? 해당 문서도 함께 알려줘",
    ),
}
DOC_VERSION = (
    "{base} 최신 버전 문서가 어느 거야? 그 문서에 적힌 견적 금액도 알려줘",
    "{base}의 가장 최근 버전 문서를 찾아서 금액이 얼마인지 알려줘. 문서도 함께 알려줘",
)

DEADLINE_REMINDER = {
    "day_before": (
        "{project} 프로젝트 {item} 마감 하루 전 오전 9시에 알려줘",
        "{project} {item} 제출 마감 전날 오전 9시에 리마인더 걸어줘",
    ),
    "days_before": (
        "{project} 프로젝트 {item} 마감 {days}일 전 오전 10시에 알려줘",
        "{project} {item} 마감일 {days}일 전 오전 10시에 리마인더 걸어줘",
    ),
    "hours_before": (
        "{project} 프로젝트 {item} 마감 {hours}시간 전에 알려줘",
        "{project} {item} 마감 {hours}시간 전에 리마인더 걸어줘",
    ),
}
DEADLINE_ASK = (
    "{project} 프로젝트 {item} 마감이 언제야?",
    "{project} 프로젝트 {item} 제출 마감이 언제까지야?",
)

ABSENT = {
    "person_no_meeting": (
        "{query}님과의 다음 미팅이 언제야?",
        "{query}님과 다음에 잡힌 미팅은 언제야?",
    ),
    "no_document": (
        "‘{query}’ 문서가 어느 거야?",
        "‘{query}’라는 문서를 찾아서 어느 문서인지 알려줘",
    ),
    "no_amount": (
        "{query} 프로젝트 견적 금액이 얼마로 안내됐어?",
        "{query} 프로젝트의 견적 금액이 얼마야?",
    ),
    "project_no_meeting": (
        "{query} 프로젝트 다음 미팅이 언제야?",
        "{query} 프로젝트로 다음에 잡힌 미팅은 언제야?",
    ),
}
AMBIGUOUS = {
    "person_meetings": (
        "{name}님과의 미팅 일정 알려줘",
        "{name}님과 잡힌 미팅 일정이 어떻게 돼?",
    ),
    "same_project_notes": (
        "{name} 프로젝트 회의록 알려줘",
        "{name} 프로젝트 회의록이 어느 문서야?",
    ),
    "homonym_person": (
        "{name}님과의 다음 미팅이 언제야?",
        "{name}님과 다음에 잡힌 미팅은 언제야?",
    ),
}

MEETING_REQUEST = (
    "{requester}님이 메일로 요청한 ‘{title}’을 {proposal} 캘린더에 일정으로 만들어줘. "
    "참석자와 장소도 메일에 나온 대로 넣어줘",
    "{requester}님이 메일로 보낸 ‘{title}’ 요청을 {proposal} 일정으로 잡아줘. "
    "참석자와 장소는 메일 내용대로 해줘",
)
EVENT_MOVE = (
    "{requester}님이 메일로 요청한 대로 {date} ‘{title}’ 일정 시간을 옮겨줘",
    "{requester}님 메일 요청에 맞춰서 {date} ‘{title}’ 일정을 변경해줘",
)
EVENT_CANCEL = (
    "{requester}님이 메일로 요청한 대로 {date} ‘{title}’ 일정을 취소해줘",
    "{requester}님 메일 요청에 따라 {date} ‘{title}’ 일정을 취소해줘",
)
REPLY_NEEDED = (
    "{sender}님이 보낸 ‘{subject}’ 메일에 답장 보내줘. 문의하신 내용은 확인하고 곧 회신드리겠다고 써줘",
    "{sender}님이 보낸 ‘{subject}’ 메일에 답장해줘. 문의 내용을 확인한 뒤 다시 연락드리겠다고 "
    "보내줘",
)

# 다중 턴: 첫 턴(대상 찾기)과 다음 턴(대명사로 가리키는 요청)
MT_PERSON_FIRST = (
    "{name}님과의 다음 미팅이 언제야?",
    "{name}님과 다음에 잡힌 미팅은 언제야?",
)
MT_PERSON_MIDDLE = ("그 미팅 장소는 어디야?", "그 미팅에 같이 들어가는 사람이 누구야?")
MT_PERSON_MOVE = ("그 미팅을 30분 뒤로 옮겨줘", "그 미팅 시작을 30분 늦춰서 옮겨줘")
MT_MAIL_MIDDLE = ("그 메일 누가 보낸 거야?", "그 메일은 누구한테서 온 거야?")
MT_MAIL_REPLY = {
    "deadline": (
        "그 마감일 확인했다고, 마감일도 같이 적어서 그 메일 보낸 사람한테 답장 보내줘",
        "마감일 확인했다고 그 마감일을 적어서 그 메일에 답장해줘",
    ),
    "amount": (
        "그 금액 확인했다고, 금액도 같이 적어서 그 메일 보낸 사람한테 답장 보내줘",
        "금액 확인했다고 그 금액을 적어서 그 메일에 답장해줘",
    ),
    "place": (
        "그 장소로 확인했다고, 장소도 같이 적어서 그 메일 보낸 사람한테 답장 보내줘",
        "장소 확인했다고 그 장소를 적어서 그 메일에 답장해줘",
    ),
    "decision": (
        "그 결정 사항 확인했다고, 결정 내용도 같이 적어서 그 메일 보낸 사람한테 답장 보내줘",
        "결정 사항 확인했다고 그 내용을 적어서 그 메일에 답장해줘",
    ),
}
MT_DAY_FOLLOWUP = ("그중 오후 일정만 알려줘", "그 일정들 중에서 오후에 시작하는 것만 다시 알려줘")


def pick(rng: random.Random, templates: tuple[str, ...]) -> str:
    return rng.choice(templates)
