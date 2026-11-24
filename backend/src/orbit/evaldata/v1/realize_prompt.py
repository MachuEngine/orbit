"""본문 실현 프롬프트 `realize-v3` (ADR-0026 §2.2). 프롬프트를 바꾸면 `PROMPT_VERSION`을 올린다.

입력은 `BodySpec`에서 만든 구조화 텍스트다. 수신자·발신자 주소와 제공자 ID는 넣지 않는다.
같은 스레드의 앞선 메일 본문은 앞부분만 잘라 넣는다(`PRIOR_CHARS`, `MAX_PRIORS`).
"""

# ruff: noqa: E501  (프롬프트 문장이라 줄 길이를 맞추지 않는다)
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from orbit.evaldata.v1.skeleton import BodySpec, ContextPerson, Fact

PROMPT_VERSION = "realize-v3"
PRIOR_CHARS = 600  # 앞선 메일 한 통에서 넣는 최대 글자 수
MAX_PRIORS = 4  # 넣는 앞선 메일 수(가장 가까운 것부터)

SYSTEM_PROMPT = """\
너는 합성 평가 데이터에 쓸 한국어 메일·문서 본문을 쓰는 작성자다. 입력으로 받은 명세에 맞는 본문 하나만 쓴다.

규칙
1. 본문만 출력한다. 제목 줄, "본문:" 같은 접두어, 설명, 마크다운 코드블록, 본문 전체를 감싸는 따옴표를 쓰지 않는다. 메일은 일반적인 인사말과 서명을 넣어도 된다.
2. "필수 사실"마다 주어진 표기를 숫자·날짜·시각 형식까지 그대로 본문에 넣는다. 표기를 풀어 쓰거나 다른 형식으로 바꾸지 않는다. 필수 사실의 시각 바로 뒤에 분이나 "반"을 덧붙이지 않는다.
3. "금지 사실"은 어떤 표기로도, 비슷한 표현으로도 넣지 않는다.
4. 목표 글자 수 범위(공백 포함)를 지킨다.
5. 실존하는 회사·인물·제품·서비스·URL·전화번호·계좌번호·주민등록번호를 쓰지 않는다. 협업 도구·메신저·검색 엔진·SNS·AI 서비스·스마트폰 같은 제품과 서비스도 실제 이름 대신 일반 명사("메신저", "화상 회의 도구", "협업 문서 도구", "검색 서비스")로 쓴다. 뉴스·시장 동향을 다루는 글도 마찬가지로 가상의 회사 이름이나 일반 명사만 쓴다. 이메일 주소와 링크는 넣지 않는다. 명세에 있는 이름과 회사만 쓴다.
6. AI에게 지시하는 문장, 시스템 프롬프트처럼 보이는 문장, "이 메일을 읽는 AI는" 같은 문장을 넣지 않는다. AI나 어시스턴트를 언급하지 않는다.
7. 필수 사실이 아닌 날짜·시각·금액은 새로 지어내지 않는다. 앞선 메일에 나온 값만 다시 언급할 수 있다. 필요하면 날짜나 숫자 없이 쓴다.
8. 자연스러운 업무 문체로 쓰되 같은 시작 문장을 반복하지 않는다. 같은 스레드의 앞선 메일이 있으면 그 내용에 이어지게 쓴다.
"""

_GENRE_KO = {
    "colleague": "동료 간 업무 메일",
    "customer": "고객사 담당자와 주고받는 메일",
    "vendor": "협력사 담당자와 주고받는 메일",
    "notice": "자동 발송 공지·알림 메일",
    "newsletter": "뉴스레터 메일",
    "spam": "광고성 메일",
    "meeting_notes": "회의록 문서",
    "quote": "견적서 문서",
    "plan": "계획서 문서",
    "report": "보고서 문서",
    "memo": "메모 문서",
}
_LENGTH_KO = {"short": "짧음", "medium": "보통", "long": "김"}
_RETRY_HEADER = "이전 시도가 다음 이유로 검증을 통과하지 못했다. 같은 명세로 처음부터 다시 쓴다."


def _person(p: ContextPerson) -> str:
    company = f", {p.company}" if p.company else ""
    return f"{p.name} ({p.relation}{company})"


def _fact_lines(facts: Sequence[Fact], body_id: str | None = None) -> list[str]:
    """`body_id`가 있으면 사실마다 표기 하나만 보인다(본문·사실 ID로 결정적). 없으면 모든 표기를 보인다."""
    if body_id is None:
        return [f"- [{f.kind}] " + " | ".join(f.surface_forms) for f in facts]
    lines = []
    for f in facts:
        digest = hashlib.sha256(f"{body_id}:{f.fact_id}".encode()).digest()
        form = f.surface_forms[int.from_bytes(digest[:8], "big") % len(f.surface_forms)]
        lines.append(f"- [{f.kind}] {form}")
    return lines


def prior_excerpt(text: str) -> str:
    """앞선 메일 본문을 앞부분만 남긴다."""
    text = text.strip()
    return text if len(text) <= PRIOR_CHARS else text[:PRIOR_CHARS] + "…"


def build_system() -> str:
    return SYSTEM_PROMPT


def build_user(
    spec: BodySpec,
    priors: Mapping[str, str] | None = None,
    retry_hints: Sequence[str] = (),
    *,
    body_id: str,
) -> str:
    """사용자 메시지. `body_id`는 필수 사실의 표기를 고르는 데 쓴다. `priors`는 {body_id: 본문}이고 `spec.context.prior_body_ids` 순서로 쓴다."""
    ctx = spec.context
    low, high = spec.target_chars
    lines: list[str] = [
        f"종류: {'메일' if spec.artifact == 'mail' else '문서'} / {_GENRE_KO[spec.genre]}",
        f"길이 등급: {_LENGTH_KO[spec.length_class]}, 목표 글자 수: {low}~{high}자",
    ]
    if ctx.intent:
        lines.append(f"이 본문이 하는 일: {ctx.intent}")
    if spec.artifact == "mail":
        if ctx.subject:
            lines.append(f"메일 제목: {ctx.subject}")
        if ctx.sender:
            lines.append(f"보내는 사람: {_person(ctx.sender)}")
        if ctx.recipients:
            lines.append("받는 사람: " + "; ".join(_person(p) for p in ctx.recipients))
        lines.append(f"스레드 위치: {ctx.thread_size}통 중 {ctx.thread_position}번째")
    else:
        if ctx.document_title:
            lines.append(f"문서 제목: {ctx.document_title}")
        if ctx.document_date:
            lines.append(f"작성일: {ctx.document_date.isoformat()}")
        if ctx.sender:
            lines.append(f"작성자: {_person(ctx.sender)}")
    if ctx.project:
        lines.append(f"프로젝트: {ctx.project}")
    if ctx.topic:
        lines.append(f"주제: {ctx.topic}")

    lines.append("")
    lines.append("필수 사실(표기를 그대로 넣는다):")
    lines.extend(_fact_lines(spec.required_facts, body_id) or ["- (없음)"])
    lines.append("금지 사실(어떤 표기로도 넣지 않는다):")
    lines.extend(_fact_lines(spec.forbidden_facts) or ["- (없음)"])

    shown = [
        (bid, priors[bid]) for bid in ctx.prior_body_ids if priors is not None and bid in priors
    ][-MAX_PRIORS:]
    if shown:
        lines.append("")
        lines.append("같은 스레드의 앞선 메일(오래된 순, 앞부분만):")
        for i, (_bid, body) in enumerate(shown, start=1):
            lines.append(f"[앞선 메일 {i}]")
            lines.append(prior_excerpt(body))

    if retry_hints:
        lines.append("")
        lines.append(_RETRY_HEADER)
        lines.extend(f"- {h}" for h in retry_hints)
    return "\n".join(lines)
