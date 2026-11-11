"""Gmail 검색 문법의 부분집합 파서 (ADR-0008 4절, D1-6). 순수 함수이며 상태가 없다.

지원하는 조건 (모두 AND, 대소문자 무시):
- `from:값`, `to:값`: 이메일 주소나 표시 이름에 값이 부분 문자열로 들어 있으면 일치한다.
  `to:`는 받는 사람(to)과 참조(cc)를 모두 본다. Gmail이 `to:`를 To·Cc·Bcc 수신자에 적용하는
  동작을 따른 것이다(실제 Gmail 계정으로 확인한 것은 아니다. 6단계 계약 테스트에서 확인한다).
  값 `me`는 계정 본인 주소를 뜻한다.
- `subject:값`: 제목에 부분 문자열로 들어 있다.
- `after:YYYY/MM/DD`, `before:YYYY/MM/DD`: 보낸 시각이 해당 날짜 0시(UTC) 이후(포함)/이전(미포함).
  한계: Gmail은 날짜 경계를 계정 시간대로 해석하지만 v0 Sim은 UTC로 고정한다.
  시간대가 UTC가 아닌 계정에서는 경계 근처의 메일 판정이 실제 Gmail과 다를 수 있다.
- `has:attachment`: 첨부파일이 있다.
- `in:sent`, `in:inbox`, `in:drafts`: 각각 `SENT`, `INBOX`, `DRAFT` 라벨이 있다.
- 일반 키워드와 큰따옴표 구문(`"a b"`): 제목·본문·발신자(주소·이름)에 부분 문자열로 들어 있다.
  연산자 값도 큰따옴표로 감쌀 수 있다(`subject:"a b"`).

그 밖의 문법(`OR`, `-부정`, 괄호·중괄호, `label:`, `rfc822msgid:`, `is:`, 알 수 없는
`xxx:` 연산자, 형식이 틀린 날짜, 닫히지 않은 따옴표, 값이 빈 연산자)은 `InvalidQuery`다.
Gmail은 단어 단위·접두어 일치이고 Sim은 부분 문자열 일치라서, Sim이 더 많이 일치할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from orbit.connectors.contracts import Address, InvalidQuery, MailMessage

_LABELS = {"sent": "SENT", "inbox": "INBOX", "drafts": "DRAFT"}
_FORBIDDEN_CHARS = "(){}"


@dataclass(frozen=True)
class Query:
    froms: tuple[str, ...] = ()
    tos: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    after: tuple[datetime, ...] = ()  # 이 시각 이후(포함)
    before: tuple[datetime, ...] = ()  # 이 시각 이전(미포함)
    has_attachment: bool = False
    labels: tuple[str, ...] = ()  # 모두 있어야 한다
    terms: tuple[str, ...] = ()

    @property
    def wants_drafts(self) -> bool:
        return "DRAFT" in self.labels


def _tokenize(query: str) -> list[tuple[str | None, str, bool]]:
    """(연산자 또는 None, 값, 따옴표 여부) 목록. 따옴표 밖 공백으로 나눈다."""
    tokens: list[tuple[str | None, str, bool]] = []
    i, n = 0, len(query)
    while i < n:
        if query[i].isspace():
            i += 1
            continue
        op: str | None = None
        # 연산자 접두어 (따옴표 시작 전의 `이름:`)
        j = i
        while j < n and not query[j].isspace() and query[j] not in '":':
            j += 1
        if j < n and query[j] == ":":
            op = query[i:j].lower()
            if not op:
                raise InvalidQuery("연산자 이름이 비어 있다")
            i = j + 1
        if i < n and query[i] == '"':
            end = query.find('"', i + 1)
            if end == -1:
                raise InvalidQuery("닫히지 않은 큰따옴표")
            value = query[i + 1 : end]
            i = end + 1
            if i < n and not query[i].isspace():
                raise InvalidQuery("큰따옴표 구문 바로 뒤에 다른 문자가 올 수 없다")
            quoted = True
        else:
            j = i
            while j < n and not query[j].isspace():
                if query[j] == '"':
                    raise InvalidQuery("단어 중간의 큰따옴표")
                j += 1
            value = query[i:j]
            i = j
            quoted = False
        tokens.append((op, value, quoted))
    return tokens


def _parse_date(value: str) -> datetime:
    parts = value.split("/")
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts):
        raise InvalidQuery(f"날짜는 YYYY/MM/DD 형식이어야 한다: {value!r}")
    if len(parts[0]) != 4 or len(parts[1]) != 2 or len(parts[2]) != 2:
        raise InvalidQuery(f"날짜는 YYYY/MM/DD 형식이어야 한다: {value!r}")
    try:
        d = date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        raise InvalidQuery(f"없는 날짜: {value!r}") from None
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def parse_query(query: str) -> Query:
    froms: list[str] = []
    tos: list[str] = []
    subjects: list[str] = []
    after: list[datetime] = []
    before: list[datetime] = []
    labels: list[str] = []
    terms: list[str] = []
    has_attachment = False

    for op, value, quoted in _tokenize(query):
        if not quoted:
            if op is None and value == "OR":
                raise InvalidQuery("OR는 지원하지 않는다")
            if value.startswith("-"):
                raise InvalidQuery("부정(-)은 지원하지 않는다")
            if any(c in value for c in _FORBIDDEN_CHARS):
                raise InvalidQuery("괄호·중괄호는 지원하지 않는다")
        if not value.strip():
            raise InvalidQuery("빈 검색어" if op is None else f"{op}: 값이 비어 있다")
        v = value.strip().lower()
        if op is None:
            terms.append(v)
        elif op == "from":
            froms.append(v)
        elif op == "to":
            tos.append(v)
        elif op == "subject":
            subjects.append(v)
        elif op == "after":
            after.append(_parse_date(value.strip()))
        elif op == "before":
            before.append(_parse_date(value.strip()))
        elif op == "has":
            if v != "attachment":
                raise InvalidQuery(f"지원하지 않는 has: 값 {value!r}")
            has_attachment = True
        elif op == "in":
            if v not in _LABELS:
                raise InvalidQuery(f"지원하지 않는 in: 값 {value!r}")
            labels.append(_LABELS[v])
        else:
            raise InvalidQuery(f"지원하지 않는 연산자 {op}:")

    return Query(
        froms=tuple(froms),
        tos=tuple(tos),
        subjects=tuple(subjects),
        after=tuple(after),
        before=tuple(before),
        has_attachment=has_attachment,
        labels=tuple(labels),
        terms=tuple(terms),
    )


def _address_matches(needle: str, addresses: tuple[Address, ...], account_email: str) -> bool:
    if needle == "me":
        needle = account_email.lower()
    return any(
        needle in a.email.lower() or (a.name is not None and needle in a.name.lower())
        for a in addresses
    )


def matches(query: Query, message: MailMessage, account_email: str) -> bool:
    """메시지가 모든 조건을 만족하는지 판단한다. 초안 포함 여부는 호출자가 정한다."""
    senders = (message.from_,)
    recipients = message.to + message.cc
    if not all(_address_matches(f, senders, account_email) for f in query.froms):
        return False
    if not all(_address_matches(t, recipients, account_email) for t in query.tos):
        return False
    subject = message.subject.lower()
    if not all(s in subject for s in query.subjects):
        return False
    if not all(message.sent_at >= d for d in query.after):
        return False
    if not all(message.sent_at < d for d in query.before):
        return False
    if query.has_attachment and not message.has_attachment:
        return False
    if not all(label in message.labels for label in query.labels):
        return False
    body = message.body_text.lower()
    sender_text = f"{message.from_.email} {message.from_.name or ''}".lower()
    return all(t in subject or t in body or t in sender_text for t in query.terms)
