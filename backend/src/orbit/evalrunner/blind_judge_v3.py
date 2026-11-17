"""의미 변형을 섞은 Judge 보정 표본 v3. 실사용·Judge 프롬프트·v2 생성 규칙은 바꾸지 않는다.

후보의 의미 타당성은 수동 검토한다. stale/role의 새 값은 manifest와 독립적으로 답에서 뽑아
실제 도구에 있는지 검사한다. 추출기는 자연어 전체를 이해하지 않으며 아래 BASELINE_RULES가
지원 범위와 한계를 기록한다. 원문·후보·정답·개별 판정은 git 제외 runs/ 밖에 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orbit.evalrunner import blind_judge as b
from orbit.evalrunner import fact_extract as facts
from orbit.evalrunner import judge

DEFAULT_SEED = 20261118
QUOTAS = {
    "surface": 6,
    "stale_value": 5,
    "role_swap": 5,
    "unsupported_addition": 4,
    "false_action": 4,
    "false_absence": 3,
    "meaning_flip": 3,
}
BASELINE_RULES = {
    "version": "fact-extract+strict-label-place-v1",
    "facts": "기존 fact_extract의 날짜·시각·금액·호칭 붙은 한글 3글자 이름 및 장소 대조"
    "(일반 숫자·이메일 제외)",
    "source": "Judge와 같은 12,000자 배분 발췌에서 JSON 값 문자열만 추출(불완전 JSON은 원문)",
    "name_extra": "담당자·발신자·수신자 등 명시적 이름표 뒤 3글자 이름; "
    "성씨만으로 일반명사를 추정하지 않음",
    "place_extra": "structured location/place/room/장소 값 및 장소 이름표·회의실/센터 등 접미어",
    "limitations": [
        "날짜 연도 무시, 오전/오후 없는 12시 이하의 시각은 두 가능 값 허용",
        "계산값·시간대 변환을 일반적으로 증명하지 못함",
        "이름·장소 추출은 보수적 글자 규칙이라 임의 이름·장소를 모두 추출하지 못함",
        "누가/무엇의 값인지, 최신/과거, 부정, 수행하지 않은 행동의 완료 여부는 판단하지 못함",
    ],
}

_NAME_LABEL = re.compile(
    r"(?:담당자|발신자|수신자|요청자|참석자|보낸\s*사람|받는\s*사람)\s*(?:[:：]|은|는)\s*"
    r"([가-힣]{3})(?=님|씨|에게|한테|입니다|이다|이며|이에요|[\s,.·:;!?]|$)"
)
_PLACE_LABEL = re.compile(r"(?:장소|위치)\s*(?:[:：]|은|는)\s*[\"'“]?([^\n,.\"'”]{2,35})")
_PLACE_SUFFIX = re.compile(
    r"(?<![가-힣])((?:[가-힣A-Za-z0-9_-]{1,20}\s*)?(?:회의실|세미나실|미팅룸|라운지|사무실|지점|센터|캠퍼스)(?:\s*[0-9A-Z]+(?:층|호)?[A-Za-z0-9]*)?)"
)
_PLACE_FIELDS = frozenset({"location", "place", "room", "장소", "위치", "회의실"})


def normalized_answer(text: str) -> str:
    """NFKC·공백·구두점·Markdown 구분자만 정리한다. 표현/의미를 바꾸는 동의어 정리는 없다."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        char
        for char in text
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "Z"))
        and char not in "`~^+=>|"
    )


def answer_hash(text: str) -> str:
    return hashlib.sha256(normalized_answer(text).encode()).hexdigest()


def _plain_fact_text(text: str) -> str:
    return re.sub(r"[*_`~]", "", unicodedata.normalize("NFKC", text))


def _source_texts(ep: judge.Episode, *, visible: bool) -> list[str]:
    keep = judge.allocate_chars([len(r) for _, r in ep.tool_results], judge.MAX_TOOL_CHARS)
    return [
        text
        for (_, result), limit in zip(ep.tool_results, keep, strict=True)
        for text in facts.source_strings(result[:limit] if visible else result)
    ]


def _structured_places(ep: judge.Episode) -> set[str]:
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    str(key).casefold() in _PLACE_FIELDS
                    and isinstance(child, str)
                    and child.strip()
                ):
                    found.add(child.strip())
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for _, body in ep.tool_results:
        try:
            walk(json.loads(body))
        except ValueError:
            continue
    return found


def _extra_values(answer: str, ep: judge.Episode) -> list[tuple[str, str]]:
    text = _plain_fact_text(answer)
    names = {match[1] for match in _NAME_LABEL.finditer(text)}
    places = {place for place in _structured_places(ep) if b.compact(place) in b.compact(text)}
    without_labels = re.sub(r"(?:장소|위치)\s*(?:[:：]|은|는)\s*", "", text)
    for pattern, body in ((_PLACE_LABEL, text), (_PLACE_SUFFIX, without_labels)):
        for match in pattern.finditer(body):
            value = re.sub(
                r"(?:입니다|이에요|이며|이고|에서|으로|은|는|에)$", "", match[1].strip()
            ).strip()
            if value:
                places.add(value)
    return [("name", name) for name in sorted(names)] + [
        ("place", place) for place in sorted(places)
    ]


def extracted_values(answer: str, ep: judge.Episode) -> list[tuple[str, str]]:
    values = [
        ("money" if fact.kind == "amount" else fact.kind, fact.text)
        for fact in facts.extract_facts(_plain_fact_text(answer))
        if fact.kind in {"date", "time", "amount", "name"}
    ]
    return list(dict.fromkeys([*values, *_extra_values(answer, ep)]))


def _value_present(
    value: str, kind: str, texts: Sequence[str], *, strict_year: bool = False
) -> bool:
    value = _plain_fact_text(value)
    texts = [_plain_fact_text(text) for text in texts]
    if kind == "text":
        return b.compact(value) in b.compact("\n".join(texts))
    if kind in {"date", "time", "money"}:
        fact_kind = "amount" if kind == "money" else kind
        extracted = [fact for fact in facts.extract_facts(value) if fact.kind == fact_kind]
        index = facts.build_source_index(texts)
        if kind == "date" and strict_year:
            # 기준선의 연도 무시 한계와 생성 검증을 분리한다. 명시 연도는 도구에도 같아야 한다.
            years = {token for token in b.fact_tokens("date", value) if token.startswith("ymd:")}
            source_years = b.fact_tokens("date", "\n".join(texts))
            if years and not years <= source_years:
                return False
        return bool(extracted) and all(facts.is_grounded(fact, index) for fact in extracted)
    return b.fact_present(value, kind, "\n".join(texts))


@dataclass(frozen=True)
class V3Mutation:
    source_id: str
    kind: str
    answer_text: str
    mutation_note: str
    introduced_values: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, row: Mapping[str, Any]) -> V3Mutation:
        fields = ("source_id", "kind", "answer_text", "mutation_note")
        if row.get("reviewed") is not True or any(
            not isinstance(row.get(field), str) or not row[field].strip() for field in fields
        ):
            raise b.BlindError(
                "v3 후보에는 reviewed와 source_id/kind/answer_text/mutation_note가 필요하다"
            )
        if row["kind"] not in QUOTAS:
            raise b.BlindError("알 수 없는 v3 변조 유형")
        introduced = row.get("introduced_values", [])
        if not isinstance(introduced, list):
            raise b.BlindError("introduced_values는 목록이어야 한다")
        values: list[tuple[str, str]] = []
        for item in introduced:
            if (
                not isinstance(item, dict)
                or item.get("kind") not in b.KINDS | {"text"}
                or not isinstance(item.get("value"), str)
                or not item["value"].strip()
            ):
                raise b.BlindError("introduced_values에는 kind/value 문자열이 필요하다")
            values.append((item["kind"], item["value"]))
        return cls(
            row["source_id"], row["kind"], row["answer_text"], row["mutation_note"], tuple(values)
        )


def validate_mutation(ep: judge.Episode, candidate: V3Mutation) -> None:
    if answer_hash(ep.answer) == answer_hash(candidate.answer_text):
        raise b.BlindError("정규화한 변조 답이 원본과 같다")
    if candidate.kind not in {"stale_value", "role_swap"}:
        return
    introduced = [
        (kind, value)
        for kind, value in extracted_values(candidate.answer_text, ep)
        if not _value_present(value, kind, [ep.answer], strict_year=True)
    ]
    # manifest 누락으로 새 값의 실재 검사를 우회하지 못한다. 선언한 값도 별도로 모두 검사한다.
    for kind, value in dict.fromkeys([*introduced, *candidate.introduced_values]):
        if not _value_present(value, kind, _source_texts(ep, visible=False), strict_year=True):
            raise b.BlindError(f"새 {kind} 값이 실제 도구 결과에 없다")
        if not _value_present(value, kind, _source_texts(ep, visible=True), strict_year=True):
            raise b.BlindError(f"새 {kind} 값이 Judge 도구 발췌에 없다")


def validate_candidates(
    episodes: Sequence[judge.Episode], rows: Sequence[Mapping[str, Any]]
) -> tuple[list[V3Mutation], list[dict[str, str]]]:
    sources = {ep.episode_id: ep for ep in episodes}
    valid: list[V3Mutation] = []
    rejected: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        try:
            candidate = V3Mutation.parse(row)
            if candidate.source_id not in sources:
                raise b.BlindError("원본 source_id가 없다")
            identity = (candidate.source_id, candidate.kind, answer_hash(candidate.answer_text))
            if identity in seen:
                raise b.BlindError("같은 source/kind/정규화 답 후보가 겹친다")
            validate_mutation(sources[candidate.source_id], candidate)
            seen.add(identity)
            valid.append(candidate)
        except b.BlindError as exc:
            rejected.append(
                {
                    "source_id": str(row.get("source_id", "")),
                    "kind": str(row.get("kind", "")),
                    "reason": str(exc),
                }
            )
    return valid, rejected


def _assign(
    sources: Mapping[str, judge.Episode],
    candidates: Sequence[V3Mutation],
    seed: int,
    *,
    exact: bool,
    max_nodes: int = 100_000,
) -> dict[str, V3Mutation | None] | None:
    by_source: dict[str, list[V3Mutation]] = {sid: [] for sid in sources}
    for candidate in sorted(
        candidates, key=lambda c: (c.source_id, c.kind, answer_hash(c.answer_text))
    ):
        by_source[candidate.source_id].append(candidate)
    rng = random.Random(seed)  # noqa: S311  시드 고정의 동률 순서다.
    order = sorted(sources)
    rng.shuffle(order)
    rank = {sid: index for index, sid in enumerate(order)}
    originals = {sid: answer_hash(ep.answer) for sid, ep in sources.items()}
    used: set[str] = set()
    selected: dict[str, V3Mutation | None] = {}
    counts: Counter[str] = Counter()
    nodes = 0

    def options(sid: str) -> list[V3Mutation | None]:
        choices: list[V3Mutation | None] = []
        for candidate in by_source[sid]:
            cap = QUOTAS[candidate.kind] if exact or candidate.kind == "surface" else 30
            if counts[candidate.kind] < cap and answer_hash(candidate.answer_text) not in used:
                choices.append(candidate)
        choices.sort(key=lambda c: (-(QUOTAS[c.kind] - counts[c.kind]), c.kind) if c else (0, ""))
        if originals[sid] not in used:
            choices.append(None)
        return choices

    def visit(mutated: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise b.BlindError("v3 배정 탐색 상한에 닿았다. 후보를 수동 조정해야 한다")
        left = [sid for sid in order if sid not in selected]
        if mutated > 30 or mutated + len(left) < 30:
            return False
        if not left:
            return mutated == 30
        available = {
            sid: [choice for choice in options(sid) if choice is None or mutated < 30]
            for sid in left
        }
        if any(not choices for choices in available.values()):
            return False
        if (
            mutated + sum(any(c is not None for c in choices) for choices in available.values())
            < 30
        ):
            return False
        if exact:
            for kind, quota in QUOTAS.items():
                possible = sum(
                    any(c is not None and c.kind == kind for c in choices)
                    for choices in available.values()
                )
                if counts[kind] + possible < quota:
                    return False
        sid = min(left, key=lambda key: (len(available[key]), rank[key]))
        for choice in available[sid]:
            digest = answer_hash(choice.answer_text) if choice else originals[sid]
            selected[sid] = choice
            used.add(digest)
            if choice:
                counts[choice.kind] += 1
            if visit(mutated + (choice is not None)):
                return True
            if choice:
                counts[choice.kind] -= 1
            used.remove(digest)
            del selected[sid]
        return False

    return selected if visit(0) else None


def generate(
    episodes: Sequence[judge.Episode],
    human: Mapping[str, bool],
    rows: Sequence[Mapping[str, Any]],
    seed: int = DEFAULT_SEED,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    sources = {ep.episode_id: ep for ep in episodes}
    if (
        len(episodes) != 60
        or len(sources) != 60
        or any(human.get(sid) is not True for sid in sources)
    ):
        raise b.BlindError("v3에는 서로 다른 원본 60개와 모두 true인 사람 라벨이 필요하다")
    candidates, rejected = validate_candidates(episodes, rows)
    available = {kind: len({c.source_id for c in candidates if c.kind == kind}) for kind in QUOTAS}
    assignment = _assign(sources, candidates, seed, exact=True)
    exact = assignment is not None
    if assignment is None:
        assignment = _assign(sources, candidates, seed, exact=False)
    if assignment is None:
        raise b.BlindError(
            json.dumps(
                {
                    "error": "유일 답 60개·변조 30개·surface 최대6 배정 불가",
                    "available": available,
                    "rejected": rejected,
                },
                ensure_ascii=False,
            )
        )
    counts = Counter(choice.kind for choice in assignment.values() if choice)
    order = sorted(sources)
    random.Random(seed).shuffle(order)  # noqa: S311
    inputs: list[dict[str, Any]] = []
    keys: list[dict[str, Any]] = []
    for index, sid in enumerate(order, start=1):
        ep, chosen = sources[sid], assignment[sid]
        task_id = f"item-{index:02d}"
        answer = chosen.answer_text if chosen else ep.answer
        inputs.append(
            {
                "sut": "blind",
                "task_id": task_id,
                "repeat": 1,
                "cluster_id": task_id,
                "question": ep.question,
                "answer_text": answer,
                "tool_results": [
                    {"name": name, "result": result} for name, result in ep.tool_results
                ],
            }
        )
        keys.append(
            {
                "id": f"blind/{task_id}/1",
                "source_id": sid,
                "mutated": chosen is not None,
                "kind": chosen.kind if chosen else None,
                "mutation_note": chosen.mutation_note if chosen else None,
                "introduced_values": [{"kind": k, "value": v} for k, v in chosen.introduced_values]
                if chosen
                else [],
                "answer_sha256": answer_hash(answer),
            }
        )
    assert len({key["answer_sha256"] for key in keys}) == 60  # noqa: S101  배정 불변식
    summary = {
        "suite_version": "v3",
        "seed": seed,
        "requested_quotas": QUOTAS,
        "actual_quotas": {kind: counts[kind] for kind in QUOTAS},
        "available_sources": available,
        "exact_quotas": exact,
        "shortages": {k: q - counts[k] for k, q in QUOTAS.items() if counts[k] < q},
        "supplements": {k: counts[k] - q for k, q in QUOTAS.items() if counts[k] > q},
        "quota_note": None
        if exact
        else "종류별 가용 후보 또는 해시·source 배정 제약으로 목표 미달; 다른 non-surface로 보충",
        "rejected_candidates": rejected,
        "normalization": "NFKC/casefold/whitespace/punctuation/markdown-markers-v1",
        "normalized_answer_duplicates": 0,
        "baseline_rules": BASELINE_RULES,
        "human_kappa": "pending",
    }
    return inputs, keys, summary


def score_key(
    keys: Sequence[Mapping[str, Any]],
    predictions: Mapping[str, bool | None],
    cost_usd: float | None = None,
) -> dict[str, Any]:
    if any(row.get("kind") not in QUOTAS for row in keys if row.get("mutated") is True):
        raise b.BlindError("v3 정답 키의 변조 유형이 없다")
    report = b.score_key(keys, predictions)
    by_kind: dict[str, Any] = {}
    for kind in QUOTAS:
        ids = [
            str(row["id"]) for row in keys if row.get("mutated") is True and row.get("kind") == kind
        ]
        detected = sum(predictions.get(eid) is False for eid in ids)
        by_kind[kind] = {
            "n": len(ids),
            "detected": detected,
            "recall": detected / len(ids) if ids else None,
            "missing": sum(predictions.get(eid) is None for eid in ids),
        }
    non_surface = [
        str(row["id"])
        for row in keys
        if row.get("mutated") is True and row.get("kind") != "surface"
    ]
    report.update(
        {
            "per_kind": by_kind,
            "non_surface_n": len(non_surface),
            "non_surface_recall": sum(predictions.get(eid) is False for eid in non_surface)
            / len(non_surface)
            if non_surface
            else None,
            "cost_usd": cost_usd,
        }
    )
    return report


def baseline_predictions(inputs: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    """키/변조 유형/manifest를 입력받지 않는 문자열 대조 기준선이다."""
    predictions: dict[str, bool] = {}
    for row in inputs:
        eid = judge.episode_id_of(row)
        ep = judge.Episode(
            eid,
            str(row["task_id"]),
            row.get("repeat"),
            str(row.get("sut")),
            None,
            str(row.get("cluster_id", row["task_id"])),
            str(row.get("question", "")),
            str(row.get("answer_text", "")),
            tuple(
                (str(t.get("name")), str(t.get("result", ""))) for t in row.get("tool_results", [])
            ),
        )
        texts = [_plain_fact_text(text) for text in _source_texts(ep, visible=True)]
        checks = [
            supported
            for fact, supported in facts.check_text(_plain_fact_text(ep.answer), texts)
            if fact.kind in {"date", "time", "amount", "name"}
        ]
        checks.extend(
            _value_present(value, kind, texts) for kind, value in _extra_values(ep.answer, ep)
        )
        predictions[eid] = all(checks)  # 추출된 사실이 없으면 근거 없음이라는 증거도 없다.
    return predictions
