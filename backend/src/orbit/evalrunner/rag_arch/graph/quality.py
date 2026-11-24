"""추출 품질(진단 7): 정답 그래프(`graph_gold.json`) 대비 개체·관계 재현율과 정밀도.

매칭 규칙은 아래 상수와 함수로 고정한다. 바꾸면 결과를 이전과 비교할 수 없으므로 `QUALITY_VERSION`을
올린다. 정답은 메일 일부의 일부 사실만 담으므로(메일당 정답 삼중항 소수) 정밀도는 하한이다.
정밀도는 (a) 전체 추출 기준과 (b) 정답이 있는 출처 메일로 한정한 기준을 함께 낸다. 이 모듈은
수치만 계산하고 좋고 나쁨을 판정하지 않는다.

매칭 규칙 요약
- 이름 비교는 `norm`(NFKC·소문자·공백/문장부호 제거 + 호칭 하나 제거) 뒤 `keys_match`다.
  완전히 같을 때만 맞다(부분 문자열·길이 비율 규칙 없음). 호칭은 `norm`이 한 번 제거한다.
- 정답 개체는 여섯 개체 종류(`ENTITY_KINDS`)의 주어·객체만 센다. 날짜·금액은 개체가 아니다.
- 개체는 같은 출처 메일 안에서 맞아야 맞다. 종류는 비교하지 않는다.
- 값(날짜·금액) 객체는 답 채점과 같은 정규화(`answer_norm.facts_pass`)로 비교한다. 날짜는 월·일이
  같아야 하고 연도는 정답과 추출 양쪽에 있을 때만 비교한다(`2026-11-24`와 `11월 24일`은 같고
  `2025-11-24`는 다르다). 금액은 정수 원으로 비교한다(`537만 원`과 `5370000`은 같다).
  한계: `facts_pass`는 부분 문자열 비교라 추출 객체가 값을 여러 개 담으면(`11월 24일, 12월 1일`)
  정답 값이 그중 하나만 들어 있어도 맞다고 센다. 이 경우 관계 재현율·정밀도가 부풀 수 있다.
- 관계 loose: 같은 메일에서 (주어, 객체) 쌍이 정답과 맞는다. 방향은 무시한다.
- 관계 strict: loose에 더해 추출 관계 이름이 정답 관계 종류의 키워드를 포함한다
  (`PREDICATE_KEYWORDS`).
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orbit.evalrunner.rag_arch.answer_norm import facts_pass
from orbit.evalrunner.rag_arch.graph.names import squash, strip_title
from orbit.evalrunner.rag_arch.graph.prompts import ENTITY_KINDS

QUALITY_VERSION = "rag-a4-quality-v3"
VALUE_TYPES = frozenset({"date", "amount"})
PREDICATE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "deadline": ("due", "deadline", "deliver", "submit", "schedule"),
    "vendor_contact": ("contact", "vendor", "manage", "charge", "responsible", "lead", "handle"),
    "works_at": ("work", "belong", "employ", "member", "affiliat"),
    "quoted_amount": ("quote", "price", "cost", "amount", "offer", "estimate"),
    "kickoff_place": ("kickoff", "hold", "locat", "place", "meet", "venue"),
    "decision": ("decid", "decision", "agree", "adopt", "approve"),
    "deposit_amount": ("deposit", "pay", "amount", "fee", "advance"),
}


def norm(name: str) -> str:
    return strip_title(squash(name))


def keys_match(a: str, b: str) -> bool:
    """`norm`을 거친 두 이름이 완전히 같다. 부분 문자열은 맞다고 보지 않는다."""
    return bool(a) and a == b


def values_match(gold: str, found: str) -> bool:
    """날짜·금액 값을 `answer_norm`의 정규형으로 비교한다. 단위 없는 숫자는 원으로 본다."""
    if not gold.strip() or not found.strip():
        return False
    return facts_pass([[gold]], found) or facts_pass([[gold]], f"{found}원")


def predicate_kind(gold_predicate: str) -> str:
    return gold_predicate.split(":", 1)[0]


def predicate_matches(kind: str, found: str) -> bool:
    low = found.lower()
    return any(k in low for k in PREDICATE_KEYWORDS.get(kind, ()))


@dataclass(frozen=True)
class FoundRelation:
    mail: str
    subject: str
    predicate: str
    object: str
    object_is_value: bool


@dataclass(frozen=True)
class FoundEntity:
    name: str
    kind: str
    mails: tuple[str, ...]


def _pair_match(gold: Mapping[str, Any], f: FoundRelation) -> bool:
    gs, go = str(gold["subject"]), str(gold["object"])
    value = gold["object_type"] in VALUE_TYPES

    def obj_ok(x: str, is_value: bool) -> bool:
        return values_match(go, x) if value else keys_match(norm(go), norm(x)) and not is_value

    forward = keys_match(norm(gs), norm(f.subject)) and obj_ok(f.object, f.object_is_value)
    if forward or value:
        return forward
    return keys_match(norm(gs), norm(f.object)) and keys_match(norm(go), norm(f.subject))


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def evaluate_quality(
    gold: Iterable[Mapping[str, Any]],
    entities: Sequence[FoundEntity],
    relations: Sequence[FoundRelation],
    *,
    mails_total: int,
    format_errors: int,
) -> dict[str, Any]:
    """한 계정의 재현율·정밀도. `gold`는 그 계정의 삼중항, 추출은 그 계정 그래프다."""
    triples = list(gold)
    gold_mails = {m for t in triples for m in t["sources"]}
    # 개체: (메일, 정규화 이름) 단위
    gold_ents: set[tuple[str, str]] = set()
    for t in triples:
        for m in t["sources"]:
            gold_ents.add((m, norm(str(t["subject"]))))
            if t["object_type"] in ENTITY_KINDS:
                gold_ents.add((m, norm(str(t["object"]))))
    found_ents = {(m, norm(e.name)) for e in entities for m in e.mails}
    ent_recall = sum(
        any(m == fm and keys_match(k, fk) for fm, fk in found_ents) for m, k in gold_ents
    )
    ent_prec_all = sum(
        any(m == gm and keys_match(k, gk) for gm, gk in gold_ents) for m, k in found_ents
    )
    found_in_gold_mails = {(m, k) for m, k in found_ents if m in gold_mails}
    ent_prec_scoped = sum(
        any(m == gm and keys_match(k, gk) for gm, gk in gold_ents) for m, k in found_in_gold_mails
    )
    by_mail: dict[str, list[FoundRelation]] = defaultdict(list)
    for r in relations:
        by_mail[r.mail].append(r)
    rec_loose = rec_strict = 0
    for t in triples:
        cands = [r for m in t["sources"] for r in by_mail.get(m, [])]
        hit = [r for r in cands if _pair_match(t, r)]
        rec_loose += bool(hit)
        kind = predicate_kind(str(t["predicate"]))
        rec_strict += any(predicate_matches(kind, r.predicate) for r in hit)
    gold_by_mail: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in triples:
        for m in t["sources"]:
            gold_by_mail[m].append(t)
    scoped = [r for r in relations if r.mail in gold_mails]
    prec_loose = sum(any(_pair_match(t, r) for t in gold_by_mail[r.mail]) for r in scoped)
    prec_strict = sum(
        any(
            _pair_match(t, r)
            and predicate_matches(predicate_kind(str(t["predicate"])), r.predicate)
            for t in gold_by_mail[r.mail]
        )
        for r in scoped
    )
    return {
        "quality_version": QUALITY_VERSION,
        "gold_triples": len(triples),
        "gold_mails": len(gold_mails),
        "mails_total": mails_total,
        "format_errors": format_errors,
        "format_error_rate": _ratio(format_errors, mails_total),
        "entity": {
            "gold": len(gold_ents),
            "recall": _ratio(ent_recall, len(gold_ents)),
            "found": len(found_ents),
            "precision_all": _ratio(ent_prec_all, len(found_ents)),
            "found_in_gold_mails": len(found_in_gold_mails),
            "precision_gold_mails": _ratio(ent_prec_scoped, len(found_in_gold_mails)),
        },
        "relation": {
            "gold": len(triples),
            "recall_loose": _ratio(rec_loose, len(triples)),
            "recall_strict": _ratio(rec_strict, len(triples)),
            "found": len(relations),
            "found_in_gold_mails": len(scoped),
            "precision_gold_mails_loose": _ratio(prec_loose, len(scoped)),
            "precision_gold_mails_strict": _ratio(prec_strict, len(scoped)),
        },
        "note": "정밀도는 정답이 메일 일부 사실만 담아 하한이다",
    }
