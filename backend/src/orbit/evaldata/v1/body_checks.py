"""생성된 본문의 결함 검사 (평가 v1 ③ 자동 검수에서 드러난 체계적 결함).

LLM이 쓴 합성 본문에는 세계 기준 연도가 아닌 연도 표기(예: 발신일은 2026년인데 "2024년 5월 15일"),
확정 표현과 다른 값을 함께 쓴 문장, 예상 금액, 자리표시자 금액이 섞일 수 있다. 질문·정답이 그 본문에
기대고 있으면 검수자가 정답 근거를 확인할 수 없으므로, 생성기가 해당 시나리오·주제를 버린다.
이 모듈은 문자열만 다루고 파일을 읽거나 LLM을 부르지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from orbit.evaldata.v1.facts import norm

# "2024년", "2024-07-31" 같은 연도 표기. 문서 번호(20261023-001)와 금액은 걸리지 않는다.
_YEAR = re.compile(r"(?<![\d.,])((?:19|20)\d{2})(?=\s*년|[-./]\d{1,2}[-./]\d{1,2})")
_VALUE = {
    "amount": re.compile(r"\d[\d,]*\s*(?:만\s*)?원"),
    "deadline": re.compile(r"\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}/\d{1,2}"),
    "date": re.compile(r"\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}/\d{1,2}"),
}
_CONFIRM = re.compile(r"최종|확정|완료")
_ESTIMATE = re.compile(r"예상|잠정")
_PLACEHOLDER = re.compile(r"(?<![\d,])0{3}\s*원|[xX○□]{2,}\s*원")
_SENTENCE_BREAK = re.compile(r"[.\n!?]")
ESTIMATE_WINDOW = 20  # 정답 값 앞뒤로 "예상"·"잠정"을 찾는 글자 수


def foreign_years(text: str, world_year: int) -> list[str]:
    """본문에 나온 연도 중 `world_year`가 아닌 것(등장 순서, 중복 제거)."""
    seen: list[str] = []
    for m in _YEAR.finditer(text):
        if int(m.group(1)) != world_year and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def confirmed_other_value(text: str, kind: str, surface_forms: Sequence[str]) -> str | None:
    """ "최종"·"확정"·"완료"가 든 문장에 정답 값과 다른 값이 있으면 그 문장(앞 60자). 없으면 None.

    등급 1 문서가 확정 표현과 함께 다른 값을 쓰면 검수자가 그 문서를 정답으로 읽을 수 있다.
    """
    value = _VALUE.get(kind)
    if value is None:
        return None
    forms = [norm(f) for f in surface_forms]
    for sentence in _SENTENCE_BREAK.split(text):
        if not _CONFIRM.search(sentence):
            continue
        for m in value.finditer(sentence):
            found = norm(m.group(0))
            if not any(f in found or found in f for f in forms):
                return sentence.strip()[:60]
    return None


def unfirm_answer(text: str, surface_forms: Sequence[str]) -> str | None:
    """정답 값 근처에 "예상"·"잠정"이 붙었거나 금액이 자리표시자("000원")면 그 사유. 없으면 None."""
    for form in surface_forms:
        for m in re.finditer(re.escape(form), text):
            near = text[max(0, m.start() - ESTIMATE_WINDOW) : m.end() + ESTIMATE_WINDOW]
            if _ESTIMATE.search(near):
                return "정답 값 근처에 예상·잠정 표현"
    if _PLACEHOLDER.search(text):
        return "금액이 자리표시자"
    return None


def topic_body_defect(
    bodies: Mapping[str, tuple[int, str]],
    fact_kind: str,
    surface_forms: Sequence[str],
    world_year: int,
) -> str | None:
    """검색 주제의 본문 결함 사유 코드. 없으면 None.

    `bodies`는 본문 ID -> (등급, 본문). 등급 1 이상의 본문만 본다. 사유 코드는 다음 중 하나다.
    - `foreign_year`: 본문에 세계 기준 연도가 아닌 연도 표기
    - `grade1_confirmed_other_value`: 등급 1 본문이 확정 표현과 함께 다른 값을 씀
    - `grade2_estimate`: 등급 2 본문의 정답 값 근처에 예상·잠정
    - `grade2_placeholder`: 등급 2 본문의 금액이 자리표시자
    """
    for _, (grade, text) in sorted(bodies.items()):
        if grade >= 1 and foreign_years(text, world_year):
            return "foreign_year"
    for _, (grade, text) in sorted(bodies.items()):
        if grade == 1 and confirmed_other_value(text, fact_kind, surface_forms):
            return "grade1_confirmed_other_value"
        if grade == 2:
            reason = unfirm_answer(text, surface_forms)
            if reason is not None:
                return "grade2_placeholder" if "자리표시자" in reason else "grade2_estimate"
    return None
