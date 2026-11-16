"""답 문장의 사실 후보 추출과 정규화 (ADR-0029 §2, 사실 근거율의 코드 부분). 순수 함수만 둔다.

답 문장에서 날짜·시각·금액·숫자·이메일·사람 이름 후보를 뽑아 정규화하고, 같은 규칙으로 근거 텍스트
(도구 결과·사용자 발화)에서 뽑은 값과 대조한다. 규칙은 이 파일 한곳에 둔다.

정규화 규칙
- 모든 텍스트는 NFKC로 바꾼다(전각 숫자·기호 정리). 이메일은 소문자로 비교한다.
- 날짜: `2026-11-16`, `2026.11.16`, `2026/11/16`, `2026년 11월 16일`, `11월 16일`, `11/16`을 월·일
  `(11, 16)`으로 맞춘다. 연도는 비교하지 않는다. `11월`·`16일`처럼 한쪽만 있는 표기와 "내일" 같은
  상대 표현은 후보로 뽑지 않는다.
- 시각: `14:30`, `오후 2시 30분`, `2시 반`, `9시`, `정오`, `자정`을 24시간제 `(시, 분)` 가능
  값의 집합으로
  맞춘다. 오전·오후 표시가 없고 12시 이하이면 `{(h, m), (h+12, m)}` 둘 다 가능하다. 후보와 근거의
  가능 값이 하나라도 겹치면 근거 있음이다. `10시간` 같은 기간은 시각이 아니다. ISO 시각의 시간대
  오프셋(`+09:00`)은 시각으로 세지 않는다.
- 금액: `300만원`, `30만 원`, `1,200,000원`, `1억 2천만 원`, `$1,200`, `100달러`를 원(또는 달러)
  정수로
  맞춘다. `만원` 앞에 숫자가 없으면 후보가 아니다.
- 숫자: 위에서 뽑힌 부분을 뺀 나머지 숫자(쉼표 제거). 목록 번호(`1.`·`2)`), 핸들(`E1`·`M3`)의 숫자,
  개수·기간 단위가 붙은 숫자(`3개`·`2건`·`4명`·`5통`·`2곳`·`3번째`·`2가지`·`30분`·`2시간`)는 후보가
  아니다. 모델이 결과를 세거나 계산해서 말하는 값이라 도구 결과에 그대로 없기 때문이다. 근거 쪽
  숫자는 근거 텍스트의
  모든 숫자열이다(관대하게 본다).
- 이메일: 소문자로 비교한다.
- 사람 이름: 흔한 성씨로 시작하는 한글 3글자 뒤에 호칭·직함(`님`, `씨`, `팀장`, `과장` 등)이
  붙은 것만
  후보다. 근거는 근거 텍스트에 그 3글자가 들어 있는가다. 호칭 없이 쓴 이름, 한글 2글자·4글자 이름,
  영문 이름은 뽑지 못한다(놓침).

한계: 글자 규칙이라 의미를 이해하지 못한다. 계산한 값(합계, 날짜 더하기)은 근거 없음으로 나온다.
서술형 주장의 근거성은 이 모듈이 아니라 Judge가 필요하다(ADR-0029 §3).
"""

import json
import re
import unicodedata
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

FACT_KINDS = ("date", "time", "amount", "number", "email", "name")

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_ISO_DATETIME = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_DATE_YMD_SEP = re.compile(r"(?<!\d)(\d{4})\s*[./]\s*(\d{1,2})\s*[./]\s*(\d{1,2})(?!\d)")
_DATE_KOREAN = re.compile(r"(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_DATE_SLASH = re.compile(
    r"(?<![\d/.])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/.%])(?!\s*(?:분기|분의|배|이상|이하|정도|쯤))"
)
_TIME_COLON = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::\d{2})?(?![\d:])")
_TIME_KOREAN = re.compile(
    r"(?:(오전|오후|아침|새벽|저녁|밤|낮)\s*)?(\d{1,2})\s*시(?!간)"
    r"(?:\s*(?:(\d{1,2})\s*분|(반)(?=$|[^가-힣]|에서|으로|부터|까지|입니다|이다|에|쯤|이|은|는|도|만)))?"
)
_TIME_WORD = re.compile(r"정오|자정")
_AMOUNT = re.compile(
    r"(?<![\d.,])((?:(?:\d[\d,]*(?:\.\d+)?)?\s*[억만천백]\s*)*(?:\d[\d,]*(?:\.\d+)?)?)\s*원"
    r"(?=$|[^가-힣]|입니다|이다|이에요|이|은|는|을|를|에|의|으로|도|만|까지|부터|씩|짜리|정도|쯤)"
)
_AMOUNT_TOKEN = re.compile(r"(\d[\d,]*(?:\.\d+)?)?\s*([억만천백])")
_AMOUNT_TAIL = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*$")
_USD = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)|(?<![\d,.])(\d[\d,]*(?:\.\d+)?)\s*(?:달러|USD)", re.IGNORECASE
)
_NUMBER = re.compile(r"(?<![A-Za-z\d.,])\d[\d,]*(?:\.\d+)?(?![\d])")
_COUNTER = re.compile(r"\s*(?:개월|개|건|명|통|곳|번째|가지|분|시간|일간|주간)")
_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)]\s")
_SURNAMES = (
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구나민진지엄채원천"
    "방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국"
)
_TITLES = "님|씨|팀장|과장|차장|부장|대리|사원|매니저|이사|대표|리더|선임|책임|교수|박사"
_NAME = re.compile(rf"(?<![가-힣])([{_SURNAMES}][가-힣]{{2}})\s?(?:{_TITLES})")

_UNIT = {"억": Decimal(10**8), "만": Decimal(10**4), "천": Decimal(1000), "백": Decimal(100)}
_AM = {"오전", "아침", "새벽"}
_PM = {"오후", "저녁", "밤", "낮"}
_FILL = " "


@dataclass(frozen=True)
class Fact:
    kind: str
    key: Hashable  # 정규화한 값(날짜 (월, 일), 시각 가능 값 집합, 금액, 숫자·이메일·이름 문자열)
    text: str  # 답에 쓰인 원래 표기


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _mask(text: str, match: re.Match[str]) -> str:
    return text[: match.start()] + _FILL * (match.end() - match.start()) + text[match.end() :]


def _date_key(month: str, day: str) -> tuple[int, int] | None:
    m, d = int(month), int(day)
    return (m, d) if 1 <= m <= 12 and 1 <= d <= 31 else None


def _time_possibilities(
    hour: int, minute: int, meridiem: str | None, *, exact: bool = False
) -> frozenset[tuple[int, int]] | None:
    """24시간제 `(시, 분)` 가능 값. `exact`면 24시간제 표기(`14:30`)라 그대로 하나다."""
    if not 0 <= minute <= 59 or not 0 <= hour <= 24:
        return None
    if exact:
        return frozenset({(hour % 24, minute)})
    if hour == 24:
        return frozenset({(0, minute)}) if minute == 0 else None
    if meridiem in _AM:
        return frozenset({(hour % 12, minute)}) if hour <= 12 else None
    if meridiem in _PM:
        return frozenset({(hour % 12 + 12, minute)}) if hour <= 12 else None
    if 1 <= hour <= 12:
        return frozenset({(hour % 12, minute), (hour % 12 + 12, minute)})
    return frozenset({(hour, minute)})


def _amount_value(segment: str) -> Decimal | None:
    """`3천5백만`·`1억 2천만`·`300`처럼 단위가 섞인 한국어 금액을 정수로 계산한다.

    천·백은 현재 묶음(만·억 앞)에 더하고, 만·억을 만나면 묶음을 곱해 합계에 넣는다."""
    if not re.search(r"\d", segment):
        return None
    total = section = Decimal(0)
    pos = 0
    try:
        for m in _AMOUNT_TOKEN.finditer(segment):
            num = Decimal(m.group(1).replace(",", "")) if m.group(1) else None
            unit = m.group(2)
            if unit in ("천", "백"):
                section += (num if num is not None else Decimal(1)) * _UNIT[unit]
            else:
                total += (section + (num if num is not None else Decimal(0))) * _UNIT[unit]
                if num is None and section == 0:
                    total += _UNIT[unit]  # 단독 `만`은 1만
                section = Decimal(0)
            pos = m.end()
        tail = _AMOUNT_TAIL.search(segment[pos:])
        if tail:
            section += Decimal(tail.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return total + section


def _canonical_amount(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _canonical_number(text: str) -> str:
    cleaned = text.replace(",", "")
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned


def extract_facts(text: str) -> list[Fact]:
    """문장에서 사실 후보를 뽑는다. 같은 구간을 두 번 세지 않는다(뽑은 구간은 지우고 다음 규칙을
    쓴다)."""
    work = _normalize(text)
    facts: list[Fact] = []

    def take(pattern: re.Pattern[str]) -> list[re.Match[str]]:
        nonlocal work
        found = list(pattern.finditer(work))
        for m in reversed(found):
            work = _mask(work, m)
        return found

    for m in take(_EMAIL):
        facts.append(Fact("email", m.group(0).casefold(), m.group(0)))
    for m in take(_ISO_DATETIME):
        date = _date_key(m.group(2), m.group(3))
        if date is not None:
            facts.append(Fact("date", date, m.group(0)))
        if m.group(4) is not None:
            poss = _time_possibilities(int(m.group(4)), int(m.group(5)), None, exact=True)
            if poss is not None:
                facts.append(Fact("time", poss, m.group(0)))
    for pattern in (_DATE_YMD_SEP, _DATE_KOREAN):
        for m in take(pattern):
            date = _date_key(m.group(2), m.group(3))
            if date is not None:
                facts.append(Fact("date", date, m.group(0)))
    for m in take(_AMOUNT):
        value = _amount_value(m.group(1))
        if value is not None:
            facts.append(Fact("amount", ("KRW", _canonical_amount(value)), m.group(0)))
    for m in take(_USD):
        raw = m.group(1) or m.group(2)
        facts.append(Fact("amount", ("USD", _canonical_number(raw)), m.group(0)))
    for m in take(_TIME_COLON):
        poss = _time_possibilities(int(m.group(1)), int(m.group(2)), None, exact=True)
        if poss is not None:
            facts.append(Fact("time", poss, m.group(0)))
    for m in take(_TIME_KOREAN):
        minute = int(m.group(3)) if m.group(3) else 30 if m.group(4) else 0
        poss = _time_possibilities(int(m.group(2)), minute, m.group(1))
        if poss is not None:
            facts.append(Fact("time", poss, m.group(0)))
    for m in take(_TIME_WORD):
        facts.append(
            Fact("time", frozenset({(12, 0)} if m.group(0) == "정오" else {(0, 0)}), m.group(0))
        )
    for m in take(_DATE_SLASH):
        date = _date_key(m.group(1), m.group(2))
        if date is not None:
            facts.append(Fact("date", date, m.group(0)))
    for m in _NAME.finditer(work):
        facts.append(Fact("name", m.group(1), m.group(1)))
    take(_LIST_MARKER)
    for m in _NUMBER.finditer(work):
        if _COUNTER.match(work, m.end()):
            continue
        facts.append(Fact("number", _canonical_number(m.group(0)), m.group(0)))
    return facts


@dataclass(frozen=True)
class SourceIndex:
    """근거 텍스트에서 뽑은 정규화 값. 같은 규칙으로 만든다."""

    dates: frozenset[tuple[int, int]]
    times: frozenset[tuple[int, int]]
    amounts: frozenset[Hashable]
    numbers: frozenset[str]
    emails: frozenset[str]
    text: str  # 이름 대조용(NFKC, 공백 정리)


def source_strings(raw: str) -> list[str]:
    """도구 결과 본문(JSON 문자열)에서 문자열 값을 모은다. JSON이 아니면 원문 그대로 쓴다.

    키 이름은 근거가 아니다. 숫자·불리언 값은 `str`로 바꿔 담는다."""
    try:
        data = json.loads(raw)
    except ValueError:
        return [raw]

    def walk(value: Any) -> Iterable[str]:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, int | float):
            yield str(value)
        elif isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for v in value.values():
                yield from walk(v)
        elif isinstance(value, list | tuple):
            for v in value:
                yield from walk(v)

    return list(walk(data))


def build_source_index(texts: Iterable[str]) -> SourceIndex:
    dates: set[tuple[int, int]] = set()
    times: set[tuple[int, int]] = set()
    amounts: set[Hashable] = set()
    numbers: set[str] = set()
    emails: set[str] = set()
    parts: list[str] = []
    for raw in texts:
        text = _normalize(raw)
        parts.append(text)
        for f in extract_facts(text):
            if f.kind == "date":
                dates.add(f.key)  # type: ignore[arg-type]
            elif f.kind == "time":
                times |= f.key  # type: ignore[arg-type]
            elif f.kind == "amount":
                amounts.add(f.key)
            elif f.kind == "email":
                emails.add(f.key)  # type: ignore[arg-type]
        numbers.update(_canonical_number(n.group(0)) for n in _NUMBER.finditer(text))
        numbers.update(_canonical_number(n) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text))
    return SourceIndex(
        frozenset(dates),
        frozenset(times),
        frozenset(amounts),
        frozenset(numbers),
        frozenset(emails),
        "\n".join(parts),
    )


def is_grounded(fact: Fact, index: SourceIndex) -> bool:
    if fact.kind == "date":
        return fact.key in index.dates
    if fact.kind == "time":
        return bool(fact.key & index.times)  # type: ignore[operator]
    if fact.kind == "amount":
        return fact.key in index.amounts
    if fact.kind == "number":
        return fact.key in index.numbers
    if fact.kind == "email":
        return fact.key in index.emails
    if fact.kind == "name":
        return str(fact.key) in index.text
    raise ValueError(f"알 수 없는 사실 종류: {fact.kind}")


def check_text(answer: str, sources: Iterable[str]) -> list[tuple[Fact, bool]]:
    """답 문장의 사실 후보와 근거 여부. `sources`는 근거 텍스트(이미 문자열로 푼 것)다."""
    index = build_source_index(sources)
    return [(f, is_grounded(f, index)) for f in extract_facts(answer)]
