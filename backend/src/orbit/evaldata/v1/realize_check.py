"""생성된 본문의 코드 검증 (ADR-0026 §2.3). 순수 함수만 둔다.

검사 항목과 실패 사유 코드(`REASONS`)
- `empty`: 빈 문자열.
- `missing_required`: 필수 사실의 표기가 하나도 본문에 없다. 판정은 `scoring.contains`와 같은
  경계 규칙이다("10시"는 "10시 30분"에 일치하지 않는다).
- `forbidden_present`: 금지 사실의 표기가 하나라도 있다.
- `too_short`·`too_long`: 글자 수가 목표 범위 [0.6×최소, 1.4×최대] 밖이다(공백 포함, 앞뒤
  공백 제외).
- `email_domain`: 허용 도메인(`example.com`·`example.org`·`example.net`·`.test`) 밖의 이메일 주소.
- `url`: URL 또는 도메인 주소.
- `phone`·`rrn`·`account_number`: 전화번호·주민등록번호·계좌번호 형식.
- `code_block`·`meta_prefix`·`wrapped_quotes`: 마크다운 코드블록, "본문:" 같은 접두어,
  전체를 따옴표로 감싼 본문.
- `injection_phrase`: AI·시스템 프롬프트·지시 무시 같은 표현(안전 세트는 따로 만든다).

앞뒤 공백은 정리해서(`CheckResult.text`) 돌려준다. 실패 사유에는 본문 원문을 담지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from orbit.evaldata.v1.skeleton import BodySpec
from orbit.evalrunner.scoring import contains

LENGTH_LOW_FACTOR = 0.6
LENGTH_HIGH_FACTOR = 1.4

REASONS: tuple[str, ...] = (
    "empty",
    "missing_required",
    "forbidden_present",
    "too_short",
    "too_long",
    "email_domain",
    "url",
    "phone",
    "rrn",
    "account_number",
    "code_block",
    "meta_prefix",
    "wrapped_quotes",
    "injection_phrase",
)

_ALLOWED_DOMAINS = ("example.com", "example.org", "example.net")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)")
_URL = re.compile(r"(?i)\bhttps?://|\bwww\.|\bftp://")
_BARE_DOMAIN = re.compile(
    r"(?i)(?<![A-Za-z0-9@.\-])[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*"
    r"\.(?:com|net|org|io|kr|dev|app|ai|co|me|info|biz|edu|gov)(?![A-Za-z0-9\-])"
)
_PHONE = re.compile(
    r"(?<![\d\-])(?:"
    r"\+82[\s\-]?\d{1,2}[\s\-]?\d{3,4}[\s\-]?\d{4}"
    r"|0\d{1,2}[\s\-]\d{3,4}[\s\-]\d{4}"
    r"|01[016789]\d{7,8}"
    r"|0[2-6]\d?\d{7,8}"
    r"|1[5-8]\d{2}[\s\-]\d{4}"
    r")(?![\d\-])"
)
_RRN = re.compile(r"(?<!\d)\d{6}[\s\-][1-8]\d{6}(?!\d)")
_ACCOUNT = re.compile(
    r"(?<![\d\-])\d{3,6}-\d{2,6}-\d{4,8}(?:-\d{1,4})?(?![\d\-])"
    r"|계좌(?:\s*번호)?\s*[:：]?\s*\d[\d\-]{7,}"
)
_CODE_BLOCK = re.compile(r"```|~~~")
_META_PREFIX = re.compile(
    r"^\s*(?:\*\*)?(?:본문|제목|메일\s*본문|문서\s*본문|내용|출력|body|subject|output)"
    r"(?:\*\*)?\s*[:：]",
    re.IGNORECASE,
)
_QUOTE_PAIRS = (('"', '"'), ("“", "”"), ("'", "'"), ("‘", "’"), ("「", "」"))
# 독자 AI에게 말을 거는 형태만 본다. "AI 기반 분석"처럼 업무 글에 흔한 단어 단독은 검출하지 않는다.
_TARGET = r"(?:(?<![A-Za-z])AI(?![A-Za-z])|어시스턴트|챗봇|(?i:assistant|chatbot))"
_PARTICLE = r"(?:이|가|는|에게|께|야|님)"
# 수신자에게 내리는 명령형만 본다. "~에 따라", "알려드립니다", "보내드립니다"는 정상 업무 문장이다.
_VERBS = (
    r"(?:무시(?:하|해)|지시를\s*(?:무시|따르|따라)|명령|따르(?:세요|라|십시오)"
    r"|(?:출력|전달|실행|보내|알려)(?:해|하라|하세요|해라|라|줘|주세요))"
)
_INJECTION = re.compile(
    r"시스템\s*프롬프트|무시하고|(?i:ignore\s+(?:all|previous|the\s+above))"
    rf"|이\s*(?:메일|문서|글)을\s*읽는\s*{_TARGET}"
    rf"|{_TARGET}{_PARTICLE}[^.!?\n]{{0,40}}{_VERBS}"
)

_HINTS = {
    "empty": "본문이 비어 있었다",
    "too_short": "본문이 목표 글자 수보다 짧았다",
    "too_long": "본문이 목표 글자 수보다 길었다",
    "email_domain": "허용되지 않은 이메일 주소가 있었다(이메일 주소는 쓰지 않는다)",
    "url": "URL이나 도메인 주소가 있었다(링크를 넣지 않는다)",
    "phone": "전화번호 형식이 있었다",
    "rrn": "주민등록번호 형식이 있었다",
    "account_number": "계좌번호 형식이 있었다",
    "code_block": "마크다운 코드블록이 있었다",
    "meta_prefix": "본문 앞에 '본문:' 같은 접두어가 있었다",
    "wrapped_quotes": "본문 전체를 따옴표로 감쌌다",
    "injection_phrase": "AI나 시스템 프롬프트를 가리키는 표현이 있었다",
}


@dataclass(frozen=True)
class CheckResult:
    text: str  # 앞뒤 공백을 정리한 본문
    reasons: tuple[str, ...] = ()
    # 재시도 메시지에 덧붙일 짧은 설명(사실의 표기는 명세에서 온 값이라 본문 원문이 아니다)
    hints: tuple[str, ...] = field(default=(), compare=False)

    @property
    def ok(self) -> bool:
        return not self.reasons


def length_bounds(target_chars: tuple[int, int]) -> tuple[int, int]:
    low, high = target_chars
    return round(low * LENGTH_LOW_FACTOR), round(high * LENGTH_HIGH_FACTOR)


def _email_allowed(domain: str) -> bool:
    d = domain.lower()
    return d.endswith(".test") or any(d == a or d.endswith("." + a) for a in _ALLOWED_DOMAINS)


def _external_refs(text: str) -> list[str]:
    reasons: list[str] = []
    emails = list(_EMAIL.finditer(text))
    if any(not _email_allowed(m.group(1)) for m in emails):
        reasons.append("email_domain")
    # 이메일 주소(허용 도메인 포함)는 URL·도메인 검사에서 뺀다.
    rest = _EMAIL.sub(" ", text)
    if _URL.search(rest) or any(
        not _email_allowed(m.group(0)) for m in _BARE_DOMAIN.finditer(rest)
    ):
        reasons.append("url")
    if _RRN.search(text):
        reasons.append("rrn")
    if _PHONE.search(text):
        reasons.append("phone")
    # 전화번호로 읽히는 숫자열은 계좌번호 검사에서 뺀다.
    if _ACCOUNT.search(_PHONE.sub(" ", _RRN.sub(" ", text))):
        reasons.append("account_number")
    return reasons


def _wrapped(text: str) -> bool:
    return len(text) >= 2 and any(text[0] == a and text[-1] == b for a, b in _QUOTE_PAIRS)


def check_body(spec: BodySpec, raw: str | None) -> CheckResult:
    """`raw`가 `spec`의 모든 검사를 통과하는지 본다. 실패 사유는 `REASONS`의 코드다."""
    text = (raw or "").strip()
    if not text:
        return CheckResult(text="", reasons=("empty",), hints=(_HINTS["empty"],))

    reasons: list[str] = []
    hints: list[str] = []

    def add(code: str, hint: str | None = None) -> None:
        if code not in reasons:
            reasons.append(code)
        hints.append(hint or _HINTS[code])

    for fact in spec.required_facts:
        if not any(contains(text, form) for form in fact.surface_forms):
            forms = " / ".join(fact.surface_forms)
            add(
                "missing_required",
                f"필수 사실이 빠졌다. 다음 표기 중 하나를 그대로 넣는다: {forms}",
            )
    for fact in spec.forbidden_facts:
        found = [form for form in fact.surface_forms if contains(text, form)]
        if found:
            add("forbidden_present", f"금지 사실이 들어갔다: {', '.join(found)}")

    low, high = length_bounds(spec.target_chars)
    if len(text) < low:
        add("too_short", f"본문이 짧았다({len(text)}자). {low}~{high}자로 쓴다")
    elif len(text) > high:
        add("too_long", f"본문이 길었다({len(text)}자). {low}~{high}자로 쓴다")

    for code in _external_refs(text):
        add(code)
    if _CODE_BLOCK.search(text):
        add("code_block")
    if _META_PREFIX.search(text):
        add("meta_prefix")
    if _wrapped(text):
        add("wrapped_quotes")
    if _INJECTION.search(text):
        add("injection_phrase")

    return CheckResult(text=text, reasons=tuple(reasons), hints=tuple(hints))
