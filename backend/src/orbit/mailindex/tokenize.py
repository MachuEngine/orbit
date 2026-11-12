"""한국어 키워드 토큰화 (ADR-0019 §4, D4-2). Kiwi 형태소 분석으로 내용어만 남긴다.

색인할 때와 질의할 때 같은 함수를 쓴다. 토큰은 영숫자·한글만 남긴 소문자라 PostgreSQL `simple`
설정 `tsvector`·`tsquery`에 그대로 넣을 수 있다.
"""

import re
import threading
from functools import lru_cache
from typing import Any

# 명사(일반·고유·수사), 용언 어간(동사·형용사), 어근, 외국어·한자·숫자, 일련번호·이메일
_CONTENT_TAGS = frozenset(
    {"NNG", "NNP", "NR", "VV", "VA", "XR", "SL", "SH", "SN", "W_SERIAL", "W_EMAIL"}
)
_NON_ALNUM = re.compile(r"[^0-9a-z가-힣ㄱ-ㅣ一-鿿]+")
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _kiwi() -> Any:
    from kiwipiepy import Kiwi  # type: ignore[import-untyped]

    return Kiwi()


def normalize_token(token: str) -> str:
    return _NON_ALNUM.sub("", token.lower())


def tokenize(text: str) -> list[str]:
    """내용어 토큰 목록(등장 순서, 중복 포함). 내용어가 없으면 빈 목록이다."""
    if not text.strip():
        return []
    with _lock:  # Kiwi 인스턴스는 스레드 사이에 공유하지 않는다
        tokens = _kiwi().tokenize(text)
    out: list[str] = []
    for token in tokens:
        if token.tag in _CONTENT_TAGS:
            normalized = normalize_token(token.form)
            if normalized:
                out.append(normalized)
    return out
