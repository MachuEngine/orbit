"""이메일 인자의 출처 분류 (ADR-0012 2절, D6-1 규칙 2).

우선순위는 `user` > `workspace_data` > `external_content` > `model`이다.
입력 수집은 호출자(4C)가 한다.
이메일은 대소문자를 무시하고, 텍스트 안의 등장은 주소 경계(앞뒤가 주소 문자가 아님)로 찾는다.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Literal

Provenance = Literal["user", "workspace_data", "external_content", "model"]

_ADDR_CHARS = r"A-Za-z0-9_%+\-"


def _norm(value: str) -> str:
    return unicodedata.normalize("NFC", value).lower()


def _pattern(email: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![{_ADDR_CHARS}.@]){re.escape(email)}(?![{_ADDR_CHARS}@]|\.[A-Za-z0-9])"
    )


def classify_emails(
    emails: Iterable[str],
    *,
    user_utterances: Iterable[str],
    workspace_emails: Iterable[str],
    external_texts: Iterable[str],
) -> dict[str, Provenance]:
    """이메일(소문자 정규화) -> 출처. 입력 순서를 유지하고 중복은 하나로 합친다."""
    utterances = [_norm(t) for t in user_utterances]
    known = {_norm(e) for e in workspace_emails}
    externals = [_norm(t) for t in external_texts]

    result: dict[str, Provenance] = {}
    for raw in emails:
        email = _norm(raw)
        if email in result:
            continue
        pattern = _pattern(email)
        if any(pattern.search(t) for t in utterances):
            result[email] = "user"
        elif email in known:
            result[email] = "workspace_data"
        elif any(pattern.search(t) for t in externals):
            result[email] = "external_content"
        else:
            result[email] = "model"
    return result
