"""A1(Advanced) 보조 로직(ADR-0036): 재정렬 순서와 문맥 압축 검증. 순수 함수만 둔다."""

import json
import re
from collections.abc import Sequence
from typing import Any


def rerank_order(scores: Sequence[float], top_k: int) -> list[int]:
    """점수 내림차순, 동점이면 검색 순위(입력 순서) 순으로 상위 top_k의 인덱스."""
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:top_k]


def _norm(text: str) -> str:
    return " ".join(text.split())


def rerank_passage(mail: dict[str, Any]) -> str:
    """reranker에 넣는 문서 문자열(제목+본문). 길이 측정 스크립트도 이 함수를 쓴다."""
    return f"{mail.get('subject', '')}\n{mail.get('body', '')}"


def split_sentences(body: str) -> list[str]:
    """줄바꿈과 문장부호(. ? !) 뒤 공백에서 나눈 공백 정규화 문장. 소수점의 점은 나누지 않는다."""
    parts = (p for line in body.split("\n") for p in re.split(r"(?<=[.?!])\s+", line))
    return [t for p in parts if (t := _norm(p))]


def compress_body(body: str, sentences: Any) -> str | None:
    """고른 항목이 모두 원문 문장과 통째로 같으면 원문 순서로 이은 본문, 아니면 None(되돌림)."""
    if not isinstance(sentences, list):
        return None
    order: dict[str, int] = {}
    for i, sentence in enumerate(split_sentences(body)):
        order.setdefault(sentence, i)
    picked: set[str] = set()
    for item in sentences:
        text = _norm(item) if isinstance(item, str) else ""
        if text not in order:  # 빈 항목, 문자열이 아닌 항목, 문장 일부, 원문에 없는 문장
            return None
        picked.add(text)
    return " ".join(sorted(picked, key=order.__getitem__))


def parse_compression(text: str | None) -> dict[str, Any] | None:
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
