"""Deterministic, answer-neutral wording for the v1 retrieval queries."""

from __future__ import annotations

import random
from datetime import datetime

from orbit.evaldata.v1.facts import norm as normalize

# Semantically equivalent descriptions for every noun used by search_topic.
# Entries intentionally avoid every whitespace-delimited token of their source noun.
NOUN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "견적": ("가격 제안", "비용 산출 내역"),
    "납기": ("물품을 받는 때", "공급 완료 시점"),
    "계약 갱신": ("약정 연장", "체결 조건의 유효기간 늘리기"),
    "정산": ("비용을 계산해 마무리하는 절차", "대금 계산 내역"),
    "검수 일정": ("품질 확인 날짜", "완성품을 살펴보는 때"),
    "보안 점검": ("정보 보호 확인", "시스템 침해 위험 검사"),
    "교육 일정": ("학습 날짜", "사용법을 익히는 때"),
    "행사 장소": ("모임이 열리는 곳", "사람들이 모이는 공간"),
    "출시 일정": ("제품 공개 날짜", "시장에 내놓는 때"),
    "예산 승인": ("지출 계획 허가", "사용 금액 동의"),
    "라이선스 갱신": ("사용권 연장", "이용 허가 기간 연장"),
    "유지보수 비용": ("관리 작업 금액", "지속 관리에 드는 돈"),
    "시연 일정": ("데모 날짜", "기능을 공개하는 때"),
    "배송 일정": ("물건 전달 날짜", "수령 예정 시점"),
    "감사 대응": ("외부 심사 준비", "외부 검토에 대한 조치"),
    "교체 주기": ("새것으로 바꾸는 간격", "교환 시점 간격"),
    "인력 투입": ("참여 인원 배치", "작업자 배정"),
    "서버 증설": ("처리 용량 확대", "컴퓨터 자원 추가"),
    "공동 마케팅": ("함께하는 홍보 활동", "협력 판촉"),
    "사용자 교육": ("이용자 학습 과정", "사용법 안내 수업"),
}


def fact_cue(fact_kind: str) -> str:
    """Return the rendered, answer-neutral cue required for a search fact kind."""
    return "최종 금액" if fact_kind == "amount" else "확정 마감"


def format_source_time(when: datetime, precision: str) -> str:
    """Format the source's local time at the selected disambiguation precision."""
    formats = {
        "month": "%Y년 %m월",
        "date": "%Y년 %m월 %d일",
        "minute": "%Y년 %m월 %d일 %H시 %M분",
        "second": "%Y년 %m월 %d일 %H시 %M분 %S초",
    }
    try:
        return when.strftime(formats[precision])
    except KeyError as exc:
        raise ValueError(f"unknown time precision: {precision}") from exc


def missing_required_field(
    query: str,
    form: str,
    project: str,
    phrase: str,
    fact_kind: str,
    *,
    synonym: str | None = None,
    author: str = "",
    when: datetime | None = None,
    time_precision: str = "month",
) -> str | None:
    """Return the first required rendered field missing from a query, if any."""
    normalized = normalize(query)
    required = (
        [("topic_phrase", phrase)]
        if form == "keyword"
        else [("project", project), ("fact_kind", fact_cue(fact_kind))]
    )
    if form == "paraphrase_question":
        required.append(("synonym", synonym or ""))
    if form == "person_time":
        required.append(("author", author))
        required.append(("source_time", format_source_time(when, time_precision) if when else ""))
    for field, value in required:
        if not value or normalize(value) not in normalized:
            return field
    return None


def render(
    phrase: str,
    form: str,
    author: str,
    when: datetime,
    artifact_kind: str,
    fact_kind: str,
    rng: random.Random,
    *,
    project: str | None = None,
    noun: str | None = None,
    synonym: str | None = None,
    time_precision: str = "month",
) -> str:
    """Render a query without answer values; `keyword` preserves lexical baseline wording."""
    project = project or phrase
    cue = fact_cue(fact_kind)
    subject = "최종 금액" if fact_kind == "amount" else "확정 마감일"
    if form == "keyword":
        variants = [
            f"{phrase} {subject}",
            f"{phrase} {subject} 최종 안내",
            f"{phrase} 최신 {subject} 자료",
        ]
        return rng.choice(variants)
    if form == "paraphrase_question":
        description = synonym or noun or "관련 사항"
        question_subject = cue
        variants = [
            f"{project} {description} 관련 {question_subject}이 확인되는 자료를 찾아줘",
            f"{project} {description}에 관해 {question_subject}을 확인할 수 있는 자료가 뭐야?",
        ]
        return rng.choice(variants)
    if form == "person_time":
        date = format_source_time(when, time_precision)
        if artifact_kind == "document":
            variants = [
                f"{author}님 소유 자료 중 {project} {subject} 자료를 찾아줘. 수정 날짜는 {date}야",
                f"{project} {subject} 자료가 필요해. {author}님 소유이고 수정 날짜는 {date}야",
            ]
        else:
            variants = [
                f"{date}에 {author}님이 보낸 {project} {subject} 자료를 찾아줘",
                f"{project} {subject} 관련해 {author}님이 {date}에 보낸 자료가 필요해",
            ]
        return rng.choice(variants)
    raise ValueError(f"unknown query form: {form}")
