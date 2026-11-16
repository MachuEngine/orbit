"""Deterministic, answer-neutral wording for the v1 retrieval queries."""

from __future__ import annotations

import random
from datetime import datetime


def render(
    phrase: str,
    form: str,
    author: str,
    when: datetime,
    artifact_kind: str,
    fact_kind: str,
    rng: random.Random,
) -> str:
    """Render one of three forms without including the answer value."""
    subject = "최종 금액" if fact_kind == "amount" else "확정 마감일"
    if form == "keyword":
        variants = [
            f"{phrase} {subject}",
            f"{phrase} {subject} 최종 안내",
            f"{phrase} 최신 {subject} 자료",
        ]
        return rng.choice(variants)
    if form == "question":
        variants = [
            f"{phrase}의 {subject}이 확인되는 최신 자료를 찾아줘",
            f"{phrase} 관련 {subject} 최종 안내 자료가 뭐야?",
        ]
        return rng.choice(variants)
    if form == "person_time":
        date = when.strftime("%Y년 %m월")
        if artifact_kind == "document":
            variants = [
                f"{author}님 소유로 {date}에 수정된 {phrase} {subject} 문서를 찾아줘",
                f"{date}에 수정된 {author}님 소유 {phrase} {subject} 문서가 필요해",
            ]
        else:
            variants = [
                f"{author}님이 {date}에 보낸 {phrase} {subject} 자료를 찾아줘",
                f"{date} 무렵 {author}님이 다룬 {phrase} {subject} 자료가 필요해",
            ]
        return rng.choice(variants)
    raise ValueError(f"unknown query form: {form}")
