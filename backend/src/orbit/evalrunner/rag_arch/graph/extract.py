"""메일 한 통의 개체·관계 추출: 입력 렌더링과 출력 검증(순수 함수, ADR-0036 A4)."""

import json
from dataclasses import dataclass
from typing import Any

from orbit.connectors.contracts import MailMessage
from orbit.evalrunner.rag_arch.graph.names import name_key, squash, strip_title
from orbit.evalrunner.rag_arch.graph.prompts import (
    ENTITY_KINDS,
    MAX_ENTITIES_PER_MAIL,
    MAX_NAME_CHARS,
    MAX_PREDICATE_CHARS,
    MAX_RELATIONS_PER_MAIL,
)


@dataclass(frozen=True)
class Relation:
    subject_key: str
    predicate: str
    object_key: str | None  # 개체이면 키, 값이면 None
    object_value: str | None


@dataclass(frozen=True)
class Extraction:
    entities: dict[str, tuple[str, str]]  # name_key -> (표시 이름, 종류)
    relations: tuple[Relation, ...]
    dropped: int  # 해석하지 못했거나 상한을 넘어 버린 관계 수


def render_mail(message: MailMessage) -> str:
    """추출 입력. 머리글(이름만)과 본문. 모델은 제공자 ID를 보지 않는다."""
    return json.dumps(
        {
            "subject": message.subject,
            "from": message.from_.name or message.from_.email,
            "to": [a.name or a.email for a in message.to],
            "cc": [a.name or a.email for a in message.cc],
            "date": message.sent_at.isoformat(),
            "body": message.body_text,
        },
        ensure_ascii=False,
    )


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned if 0 < len(cleaned) <= limit else None


def parse_extraction(text: str | None) -> Extraction | None:
    """검증을 통과한 추출 결과. 형식 실패(JSON 오류, 구조 오류)면 None이다.

    개체는 이름 정규화 키로 중복을 합치고 종류가 알려진 것만 남긴다(첫 항목 우선). 관계는
    subject가 개체 목록에 없으면 버리고(`dropped`), object가 개체 목록에 없으면 값으로 둔다.
    """
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        return None
    if not isinstance(data.get("relations"), list):
        return None
    entities: dict[str, tuple[str, str]] = {}
    alias: dict[str, str] = {}  # squash 이름(호칭 포함·제외) -> name_key
    for item in data["entities"][:MAX_ENTITIES_PER_MAIL]:
        name = _text(item.get("name"), MAX_NAME_CHARS) if isinstance(item, dict) else None
        kind = item.get("type") if isinstance(item, dict) else None
        if name is None or kind not in ENTITY_KINDS:
            continue
        key = name_key(name, kind)
        if not key:
            continue
        entities.setdefault(key, (name, kind))
        alias.setdefault(squash(name), key)
        alias.setdefault(strip_title(squash(name)), key)
    relations: dict[Relation, None] = {}
    dropped = max(0, len(data["relations"]) - MAX_RELATIONS_PER_MAIL)
    for item in data["relations"][:MAX_RELATIONS_PER_MAIL]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        subject = _text(item.get("subject"), MAX_NAME_CHARS)
        predicate = _text(item.get("predicate"), MAX_PREDICATE_CHARS)
        obj = _text(item.get("object"), MAX_NAME_CHARS)
        s_key = None if subject is None else alias.get(squash(subject))
        if s_key is None or predicate is None or obj is None:
            dropped += 1
            continue
        o_key = alias.get(squash(obj)) or alias.get(strip_title(squash(obj)))
        relations[Relation(s_key, predicate, o_key, None if o_key else obj)] = None
    return Extraction(entities, tuple(relations), dropped)
