"""rag-v1 답 문장 코드 채점의 표기 정규화 (ADR-0036 "질문 유형과 gold").

답과 gold 후보를 같은 정규형으로 바꾼 뒤 부분 문자열로 비교한다. 규칙은 dev 결과를 보기 전에
고정했고, 바꾸면 `RAG_ANSWER_NORM_VERSION`을 올린다.

정규형은 개체(날짜·금액·수)를 사설 영역 문자로 감싼 토큰으로 바꾼다. 토큰은 고정 형식이고
문장부호 제거에서 보호해서, gold 3이 "13통"에, 금액 5,370,000이 15,370,000에, "1월 12일"이
"11월 2일"에, "3.5통"이 "35통"에 부분 일치하지 않는다.

- 날짜: `2026-11-24`, `2026.11.24`, `2026년 11월 24일`, `11/24`, `11월 24일`, `11월24일` ->
  연·월·일(+요일) 토큰. 연도와 요일은 gold에 있을 때만 비교한다. gold에 있고 답에도 있으면 같아야
  하고(2025-11-24는 gold 2026-11-24와 다르다), 답에 없으면 날짜만 비교한다. gold에 없으면 답의
  연도·요일은 무시한다. 요일은 `(화)`, `(화요일)`, ` 화요일` 표기를 읽는다.
- 금액: `5,370,000원`, `537만 원`, `오백삼십칠만 원`, `5백3십7만원` -> 정수 원 토큰.
- 수: 아라비아 수(쉼표·소수 허용), 그리고 단위(통·건·명·개·주 ...) 앞의 한글 고유어 수사
  (하나~스무: 한/두/세/네/스무 ...) -> 수 토큰. 단위는 그대로 남아 "3통"과 "3건"은 다르다.
- 공백은 경계 정보다. 정규형에는 한 칸 공백으로 남기고, 비교는 공백을 뺀 문자열 위에서 하되 각 글자
  앞에 공백이 있었는지를 함께 본다(한글 띄어쓰기 차이는 허용).
- 단위 경계: gold 후보가 수+단위로 끝나면 답에서 단위에 한글이 바로 붙어 이어질 때(조사 제외)
  일치시키지 않는다("2개"는 "2개월"에, "10시"는 "10시간"에 안 맞고 "2개 남음"에는 맞는다).
  "10시"는 "10시 30분"·"10시 반"에도 안 맞는다.
- 조건 꼬리: gold가 단위·날짜·금액·수로 끝나면 답에서 그 뒤에 이상·이하·이내·이후·이전·미만·초과·
  이외·전후·쯤·경·정도·가량이 오면(공백 유무 무관) 일치시키지 않는다. gold에 꼬리가 있으면
  답에도 있어야 한다. "까지"·"부터"는 꼬리가 아니다("11월 24일까지"는 gold "11월 24일"과 맞는다).
- 영문자로 시작·끝나는 후보는 답에서 공백 없이 영문자·숫자에 붙으면 일치시키지 않는다
  ("A"는 "Abc"·"A1"에 안 맞고 "Conference Room A"에는 맞는다).
- 문장부호는 공백으로 바꾼다(토큰 안은 제외). 의미 비교는 하지 않는다.

shortcut: 한자어 수사 단독("삼 건")과 한글 날짜("십일월 이십사일")는 다루지 않는다. 금액은 숫자나
자릿수 단위(십·백·천·만·억·조)가 있을 때만 읽고, 뒤에 오는 "원칙" 같은 낱말은 일부만 거른다.
단위 뒤 조사·조건 꼬리 목록은 고정 목록이다. 공백 무시 비교라 영문 "a b"가 "ab"와 같게 보인다.
Judge 표본(correctness.py)이 검증한다.
"""

import re
import unicodedata
from collections.abc import Iterable

RAG_ANSWER_NORM_VERSION = "rag-answer-norm-v3"

_OPEN, _CLOSE = "\ue000", "\ue001"
_NO_YEAR, _NO_WD = "----", "-"

_NATIVE = {
    "하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "석": 3, "넷": 4, "네": 4, "넉": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10, "열하나": 11, "열한": 11,
    "열둘": 12, "열두": 12, "열셋": 13, "열세": 13, "열넷": 14, "열네": 14, "열다섯": 15,
    "열여섯": 16, "열일곱": 17, "열여덟": 18, "열아홉": 19, "스물": 20, "스무": 20,
}  # fmt: skip
_COUNTERS = "개월|가지|차례|시간|시|통|건|명|개|주|곳|번|회|장|부|달|분"
# 고유어 수사 뒤 "시"는 "한 시스템" 같은 낱말을 피하려고 조사·경계가 이어질 때만 단위로 본다.
_NAT_COUNTERS = _COUNTERS.replace(
    "|시|", "|시(?:(?![가-힣])|(?=에|부터|까지|쯤|경|반|이|가|는|은|를|을|의|로|정도|가량))|"
)
_SINO = {"영": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
_SMALL = {"십": 10, "백": 100, "천": 1000}
_BIG = {"만": 10**4, "억": 10**8, "조": 10**12}
_SINO_CLASS = "영일이삼사오육칠팔구십백천만억조"

_WD = "[월화수목금토일]"


def _wd_suffix(name: str) -> str:
    # shortcut: 괄호 없는 한 글자 요일("24일 수")은 뒤에 낱말이 없을 때만 읽는다("24일 수 있다" 오독
    # 방지). "24일 수 회의" 같은 표기는 요일로 읽지 못한다.
    return (
        rf"(?P<{name}>\s*\(\s*(?P<{name}1>{_WD})(?:요일)?\s*\)|\s*(?P<{name}2>{_WD})요일"
        rf"|\s+(?P<{name}3>{_WD})(?=\s*(?:[^\w\s]|$)))?"
    )


# 개체 하나를 한 번에 찾는다. 앞쪽 대안이 같은 위치에서 먼저 시도된다.
_ENTITY = re.compile(
    r"(?P<ymd>(?<!\d)(?P<yy>\d{4})\s*(?:[-./]|년)\s*(?P<ym>\d{1,2})\s*(?:[-./]|월)\s*"
    r"(?P<yd>\d{1,2})\s*일?(?!\d)" + _wd_suffix("yw") + ")"
    r"|(?P<kmd>(?<!\d)(?P<km>\d{1,2})\s*월\s*(?P<kd>\d{1,2})\s*일" + _wd_suffix("kw") + ")"
    r"|(?P<smd>(?<![\d/.\-])(?P<sm>\d{1,2})/(?P<sd>\d{1,2})(?![\d/])" + _wd_suffix("sw") + ")"
    rf"|(?P<amt>(?:(?<![가-힣])(?=[{_SINO_CLASS}])|(?=\d))"
    rf"(?:[\d,]++|[{_SINO_CLASS}])(?:\s*+(?:[\d,]++|[{_SINO_CLASS}]))*+\s*원(?!칙|본|래|문|활|격))"
    rf"|(?P<nat>(?<![가-힣])(?:{'|'.join(sorted(_NATIVE, key=len, reverse=True))})"
    rf"(?=\s*(?:{_NAT_COUNTERS})))"
    r"|(?P<num>\d+(?:,\d{3})*(?:\.\d+)?)"
)


def _tok(kind: str, value: str) -> str:
    return f"{_OPEN}{kind}{value}{_CLOSE}"


def _won(raw: str) -> int | None:
    """한글·아라비아 혼합 금액 표기(원 제외)를 정수 원으로 바꾼다. 해석하지 못하면 None."""
    total = section = 0
    num: int | None = None
    prev_digit = False
    for m in re.finditer(rf"[\d,]+|[{_SINO_CLASS}]", raw):
        s = m.group()
        if s[0].isdigit() or s[0] == ",":
            digits = s.replace(",", "")
            if not digits:
                return None
            num, prev_digit = int(digits), False
        elif s in _SINO:
            num = (num or 0) * 10 + _SINO[s] if prev_digit else _SINO[s]
            prev_digit = True
        elif s in _SMALL:
            section += (1 if num is None else num) * _SMALL[s]
            num, prev_digit = None, False
        else:
            section += num or 0
            total += (section or 1) * _BIG[s]
            section, num, prev_digit = 0, None, False
    return total + section + (num or 0)


def _date(m: re.Match[str], year: str | None, month: str, day: str, w: str) -> str:
    mo, d = int(month), int(day)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    wd = m.group(w + "1") or m.group(w + "2") or m.group(w + "3") or _NO_WD
    return _tok("d", f"{year or _NO_YEAR}{mo:02d}{d:02d}{wd}")


def _replace(m: re.Match[str]) -> str:
    if m.group("ymd"):
        return _date(m, m.group("yy"), m.group("ym"), m.group("yd"), "yw")
    if m.group("kmd"):
        return _date(m, None, m.group("km"), m.group("kd"), "kw")
    if m.group("smd"):
        return _date(m, None, m.group("sm"), m.group("sd"), "sw")
    if m.group("amt"):
        raw = m.group("amt").rstrip().removesuffix("원")
        won = _won(raw) if re.search(r"\d|[십백천만억조]", raw) else None  # "사원"은 금액이 아니다
        return m.group(0) if won is None else _tok("w", str(won))
    if m.group("nat"):
        return _tok("n", str(_NATIVE[m.group("nat")]))
    text = m.group("num").replace(",", "")
    return _tok("n", str(float(text)) if "." in text else str(int(text))).replace(
        ".0" + _CLOSE, _CLOSE
    )


_TOKEN = re.compile(f"({_OPEN}[^{_CLOSE}]*{_CLOSE})")


def normalize_answer(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold()
    t = _ENTITY.sub(_replace, t)
    # 토큰 안은 그대로 두고 바깥의 문장부호는 공백으로, 연속 공백은 하나로 줄인다(경계 정보로 쓴다).
    parts = _TOKEN.split(t)
    out = "".join(
        part
        if i % 2
        else "".join(
            " " if c.isspace() or unicodedata.category(c).startswith("P") else c for c in part
        )
        for i, part in enumerate(parts)
    )
    return " ".join(out.split())


_DATE_TOKEN = re.compile(f"{_OPEN}d(\\d{{4}}|-{{4}})(\\d{{4}})(.){_CLOSE}")
_UNIT_END = re.compile(f"{_CLOSE}(?:{_COUNTERS}|층|일|월|년)$")
_PARTICLES = (
    "에서", "으로", "부터", "까지", "에게", "에", "로", "쯤", "이", "은", "는", "가", "을", "를",
    "의", "도", "만", "와", "과", "입", "였", "예", "인", "정도", "가량", "씩", "밖에", "라",
)  # fmt: skip
_TAILS = (
    "이상", "이하", "이내", "이후", "이전", "미만", "초과", "이외", "전후", "쯤",
    "경", "정도", "가량",
)  # fmt: skip
_MINUTES = re.compile(f"{_OPEN}n\\d+{_CLOSE}분")


def _strip(s: str, *, year: bool, wd: bool) -> str:
    """날짜 토큰에서 연도·요일을 '없음' 표시로 바꾼다."""

    def sub(m: re.Match[str]) -> str:
        y, md, w = m.groups()
        return f"{_OPEN}d{_NO_YEAR if year else y}{md}{_NO_WD if wd else w}{_CLOSE}"

    return _DATE_TOKEN.sub(sub, s)


def _has(s: str, *, year: bool) -> bool:
    return any(
        (m.group(1) != _NO_YEAR) if year else (m.group(3) != _NO_WD)
        for m in _DATE_TOKEN.finditer(s)
    )


def _is_alnum(ch: str) -> bool:
    return (ch.isascii() and ch.isalnum()) or ch in (
        _OPEN,
        _CLOSE,
    )  # 토큰은 수·날짜라 붙으면 다른 값


def _compact(s: str) -> tuple[str, list[bool]]:
    """공백을 지운 문자열과 '이 글자 앞에 공백이 있었는가' 표시를 돌려준다."""
    chars: list[str] = []
    space: list[bool] = []
    pending = False
    for c in s:
        if c.isspace():
            pending = True
        else:
            chars.append(c)
            space.append(pending)
            pending = False
    space.append(pending)
    return "".join(chars), space


def _hangul(ch: str) -> bool:
    return "\uac00" <= ch <= "\ud7a3"


def _word_end(text: str, space: list[bool], k: int) -> bool:
    """text[k]에서 낱말이 끝나는가(끝·공백·한글 아님·조사)."""
    return k >= len(text) or space[k] or not _hangul(text[k]) or text.startswith(_PARTICLES, k)


def _bounded(cand: str, text: str, space: list[bool], start: int) -> bool:
    """후보가 text[start:]에서 단위·영문·조건 꼬리 경계를 지키는가."""
    end = start + len(cand)
    glued = end < len(text) and not space[end]
    if (
        cand[0].isascii()
        and cand[0].isalpha()
        and start > 0
        and not space[start]
        and _is_alnum(text[start - 1])
    ):
        return False
    if cand[-1].isascii() and cand[-1].isalpha() and glued and _is_alnum(text[end]):
        return False
    unit = bool(_UNIT_END.search(cand))
    tail = any(text.startswith(t, end) and _word_end(text, space, end + len(t)) for t in _TAILS)
    if tail and (unit or cand.endswith(_CLOSE)):
        return False
    if unit and glued and _hangul(text[end]) and not text.startswith(_PARTICLES, end):
        return False
    if cand.endswith("시"):
        if _MINUTES.match(text, end):
            return False
        if text.startswith("반", end) and _word_end(text, space, end + 1):
            return False
    return True


def _contains(cand: str, text: str, space: list[bool]) -> bool:
    pos = 0
    while (i := text.find(cand, pos)) >= 0:
        if _bounded(cand, text, space, i):
            return True
        pos = i + 1
    return False


def _match(gold: str, answer: str) -> bool:
    """gold 후보 정규형이 답 정규형에 들어 있는가. 연도·요일은 gold에 있을 때만 비교한다."""
    has_year, has_wd = _has(gold, year=True), _has(gold, year=False)
    text, space = _compact(_strip(answer, year=not has_year, wd=not has_wd))
    for drop_year in (False, True) if has_year else (False,):
        for drop_wd in (False, True) if has_wd else (False,):  # 답에 없으면 날짜만 비교한다
            if _contains(_compact(_strip(gold, year=drop_year, wd=drop_wd))[0], text, space):
                return True
    return False


def facts_pass(facts: Iterable[Iterable[str]], answer: str) -> bool:
    """사실마다 any_of 후보 하나가 답에 들어 있어야 한다. 정규형이 빈 후보는 일치로 보지 않는다."""
    norm = normalize_answer(answer)
    return all(
        any(c and _match(c, norm) for c in map(normalize_answer, any_of)) for any_of in facts
    )
