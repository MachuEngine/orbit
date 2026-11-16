"""평가 지표 v1: 결과 행만 입력으로 받는 보조 지표 (ADR-0029 §2). 순수 함수만 둔다.

성공 판정·실패 유형 규칙은 바꾸지 않는다. 여기 지표는 모두 성공률 옆에 보는 보조 지표다.
기록(`trace`, `task_meta`, `min_tool_calls`, `change_fields`, `calls[].fault`)이 없는 옛
결과(스키마 1)는
해당 지표를 `{"available": False, "reason": ...}`로 낸다. 옛 결과와 새 결과를 한 비교에 섞지 않는다.

섹션: `accuracy_extra`(쓰기 인자 필드별, 다중 턴), `grounding`(사실 근거율·인용·기권),
`tools`(선택·인자
유효율·초과 호출), `robustness`(결함 주입·표현 변형), `efficiency`(비용·지연·호출 수).

한계(보고서에 같이 적는다)
- 정직 보고와 결함 환각은 표현 목록(`FAILURE_PHRASES`)과 구조화된 답(`abstain`)으로 판정한다.
  목록에 없는
  표현은 놓친다(정직 보고율을 낮게, 환각률은 영향 없음).
- 사실 근거율은 날짜·시각·금액·숫자·이메일·이름 후보만 본다(`fact_extract`). 서술형 주장은 보지
  않는다.
- 최소 호출 수는 정답을 이미 아는 오라클의 호출 수라 실제 필요한 호출 수보다 작거나 같다.
"""

import math
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from orbit.evalrunner.fact_extract import (
    Fact,
    SourceIndex,
    build_source_index,
    extract_facts,
    is_grounded,
    source_strings,
)
from orbit.evalrunner.scoring import normalize_text

Record = Mapping[str, Any]

# ---------------------------------------------------------------- 상수 표(한곳)

# 실패를 알리는 표현. 답 문장에 하나라도 있으면(공백 제거·NFKC 비교) 실패를 언급한 것으로 본다.
FAILURE_PHRASES: tuple[str, ...] = (
    "오류",
    "실패",
    "문제가 발생",
    "문제가 생",
    "일시적",
    "다시 시도",
    "재시도",
    "잠시 후",
    "확인하지 못",
    "확인할 수 없",
    "확인되지 않",
    "조회하지 못",
    "조회할 수 없",
    "불러오지 못",
    "가져오지 못",
    "접근할 수 없",
    "처리하지 못",
    "완료하지 못",
    "수행하지 못",
    "실행하지 못",
    "결과를 알 수 없",
    "불확실",
    "확실하지",
    "응답하지 않",
    "요청이 많",
)

# 인자·형식 오류로 보는 도구 오류 종류(`{"error": 종류}`). 모델이 잘못 낸 인자 때문에 난 오류다.
INVALID_ARGUMENT_KINDS = frozenset(
    {
        "invalid_arguments",
        "invalid_time",  # 시간대(오프셋)가 없거나 형식이 틀린 시각
        "invalid_time_range",
        "invalid_kind",
        "invalid_query",  # 지원하지 않는 검색 문법
        "unknown_handle",
        "unknown_tool",
    }
)

# 도구 이름 -> 데이터 영역. 두 평가 대상(assistant_v0, runflow_v0)과 기준 SUT(scripted_tool)의
# 이름을
# 모두 둔다. `None`이 아닌 영역만 선택·잘못된 영역 지표에 쓴다.
AREA_CALENDAR, AREA_MAIL, AREA_DOCUMENT, AREA_WRITE, AREA_REMINDER = (
    "calendar",
    "mail",
    "document",
    "write",
    "reminder",
)
TOOL_AREA: dict[str, str] = {
    "list_events": AREA_CALENDAR,
    "get_event": AREA_CALENDAR,
    "list_calendars": AREA_CALENDAR,
    "search_mail": AREA_MAIL,
    "read_mail": AREA_MAIL,
    "search_indexed_mail": AREA_MAIL,
    "search_messages": AREA_MAIL,
    "get_message": AREA_MAIL,
    "search_docs": AREA_DOCUMENT,
    "read_doc": AREA_DOCUMENT,
    "create_event": AREA_WRITE,
    "update_event_time": AREA_WRITE,
    "cancel_event": AREA_WRITE,
    "send_mail": AREA_WRITE,
    "create_reminder": AREA_REMINDER,
}
# 영역도 호출 수도 세지 않는 도구(답 제출, 맥락 조회)
IGNORED_TOOLS = frozenset({"submit_answer", "conversation_context"})

# 과제의 기대 도구 영역: 근거 태그(`ref_event` 등)가 가리키는 데이터 영역 + 유형·시나리오가
# 더하는 영역.
REF_AREA = {"event": AREA_CALENDAR, "message": AREA_MAIL, "file": AREA_DOCUMENT}
TYPE_EXTRA_AREA = {"write": AREA_WRITE, "reminder": AREA_REMINDER}
SCENARIO_EXTRA_AREA = {  # (유형, 시나리오) -> 영역. 다중 턴 쓰기 과제
    ("multi_turn", "mail_fact"): AREA_WRITE,
    ("multi_turn", "person_next"): AREA_WRITE,
}
NO_TOOL_TYPES = frozenset({"ambiguous"})  # 되묻는 과제는 기대 도구가 없다

# 정답 종류 -> 인용 비교에 쓰는 ref 종류. `time_slot`은 가리키는 아티팩트가 없다.
ANSWER_REF_KIND = {
    "event_set": "event",
    "event": "event",
    "event_time": "event",
    "message_set": "message",
    "document": "file",
}
ANSWERING_KINDS = frozenset({*ANSWER_REF_KIND, "time_slot"})

MULTI_TURN_NOTE = "대화 단위(마지막 턴까지 맞아야 성공). 턴 단위 정답이 없는 턴은 재지 않는다"


def unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


# ---------------------------------------------------------------- 과제 메타

_DERIVED_SUFFIX = {"fault": "-fault", "paraphrase": "-para"}


def task_meta(task: Any) -> dict[str, Any]:
    """과제 태그에서 결과 행에 남길 메타. 정답은 담지 않는다(근거 태그의 ID만)."""
    tags: dict[str, str] = {}
    refs: dict[str, list[str]] = {"event": [], "message": [], "file": []}
    for tag in task.tags:
        key, _, value = tag.partition(":")
        if key.startswith("ref_") and key[4:] in refs:
            refs[key[4:]].append(value)
        else:
            tags.setdefault(key, value)
    derived = tags.get("derived")
    origin: str | None = None
    suffix = _DERIVED_SUFFIX.get(derived or "")
    if suffix and task.task_id.endswith(suffix):
        origin = task.task_id[: -len(suffix)]
    return {
        "type": tags.get("type"),
        "scenario": tags.get("scenario"),
        "derived": derived,
        "origin_id": origin,
        "fault_error": tags.get("fault"),
        "refs": {k: sorted(set(v)) for k, v in refs.items()},
        "as_of": task.as_of_time.isoformat(),
        "n_turns": len(task.turns),
    }


def expected_areas(meta: Mapping[str, Any]) -> frozenset[str]:
    """과제 메타에서 기대 도구 영역을 정한다(표는 위 상수)."""
    kind, scenario = meta.get("type"), meta.get("scenario")
    if kind in NO_TOOL_TYPES:
        return frozenset()
    areas = {REF_AREA[k] for k, ids in (meta.get("refs") or {}).items() if ids and k in REF_AREA}
    if kind in TYPE_EXTRA_AREA:
        areas.add(TYPE_EXTRA_AREA[kind])
    extra = SCENARIO_EXTRA_AREA.get((kind, scenario))  # type: ignore[arg-type]
    if extra is not None:
        areas.add(extra)
    return frozenset(areas)


# ---------------------------------------------------------------- 행 접근 도우미


def _trace(r: Record) -> Mapping[str, Any] | None:
    t = r.get("trace")
    return t if isinstance(t, Mapping) else None


def _meta(r: Record) -> Mapping[str, Any] | None:
    m = r.get("task_meta")
    return m if isinstance(m, Mapping) else None


def _tool_calls(r: Record) -> list[Mapping[str, Any]]:
    t = _trace(r)
    return [c for c in (t or {}).get("tool_calls", []) if isinstance(c, Mapping)]


def _counted_calls(r: Record) -> list[Mapping[str, Any]]:
    return [c for c in _tool_calls(r) if c.get("name") not in IGNORED_TOOLS]


def _scored(records: Sequence[Record]) -> list[Record]:
    return [r for r in records if r["outcome"]["success"] is not None]


def _success(r: Record) -> bool:
    return r["outcome"]["success"] is True


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def percentile(values: Sequence[float], p: float) -> float | None:
    """nearest-rank 백분위수(p는 0~1). 값이 없으면 None."""
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(p * len(ordered)) - 1)])


def _answer_kind(r: Record) -> str | None:
    ids = r.get("answer_ids")
    kind = ids.get("kind") if isinstance(ids, Mapping) else None
    return kind if isinstance(kind, str) else None


# ---------------------------------------------------------------- 사실 근거

_NO_TRACE = "trace 기록 없음(결과 스키마 1, ADR-0029 이전 결과)"


def episode_sources(r: Record) -> list[str]:
    """한 에피소드에서 사실의 근거가 되는 텍스트: 도구 결과 본문, 사용자 발화, 과제 기준 날짜.

    사용자 발화를 근거에 넣는 이유: 답이 질문 속 날짜·이름을 되풀이하는 것은 지어낸 사실이
    아니다."""
    texts: list[str] = []
    for c in _tool_calls(r):
        if c.get("name") in IGNORED_TOOLS or c.get("is_error"):
            continue
        texts += source_strings(str(c.get("result", "")))
    trace = _trace(r) or {}
    texts += [str(t.get("user", "")) for t in trace.get("turns", []) if isinstance(t, Mapping)]
    meta = _meta(r)
    if meta and isinstance(meta.get("as_of"), str):
        # 기준 시각은 사용자 시간대의 현지 날짜만 근거로 쓴다(시각은 근거가 아니다)
        try:
            tz = ZoneInfo(str(r.get("time_zone") or "Asia/Seoul"))
            texts.append(datetime.fromisoformat(meta["as_of"]).astimezone(tz).date().isoformat())
        except (ValueError, KeyError):
            pass
    return texts


def answer_facts(r: Record) -> list[tuple[Fact, bool]] | None:
    """답 문장의 사실 후보와 근거 여부. `trace`가 없거나 답 문장이 없으면 None."""
    trace = _trace(r)
    if trace is None:
        return None
    text = trace.get("final_text")
    if not isinstance(text, str) or not text.strip():
        return None
    index: SourceIndex = build_source_index(episode_sources(r))
    return [(f, is_grounded(f, index)) for f in extract_facts(text)]


def fact_support(records: Sequence[Record]) -> dict[str, Any]:
    rows = [r for r in records if _trace(r) is not None]
    if not rows:
        return unavailable(_NO_TRACE)
    total: Counter[str] = Counter()
    grounded: Counter[str] = Counter()
    with_facts = ungrounded_episodes = 0
    per_episode: list[float] = []
    for r in rows:
        facts = answer_facts(r)
        if not facts:
            continue
        with_facts += 1
        ok = 0
        for fact, hit in facts:
            total[fact.kind] += 1
            grounded[fact.kind] += hit
            ok += hit
        ungrounded_episodes += ok < len(facts)
        per_episode.append(ok / len(facts))
    n, g = sum(total.values()), sum(grounded.values())
    return {
        "available": True,
        "episodes": len(rows),
        "episodes_with_facts": with_facts,
        "episodes_with_ungrounded_fact": ungrounded_episodes,
        "facts": n,
        "grounded": g,
        "support_rate": _rate(g, n),
        "episode_mean_support": _mean(per_episode),
        "by_kind": {
            k: {"facts": total[k], "grounded": grounded[k], "rate": _rate(grounded[k], total[k])}
            for k in sorted(total)
        },
        "scope": "날짜·시각·금액·숫자·이메일·이름 후보만 본다. 서술형 주장은 Judge 대상이다",
    }


def _cited(r: Record, ref_kind: str) -> set[str] | None:
    """답이 가리킨 아티팩트 ID(`answer_ids` 요약). 답이 없으면 None."""
    ids = r.get("answer_ids")
    if not isinstance(ids, Mapping):
        return None
    out: set[str] = set()
    if ref_kind == "event":
        raw = ids.get("events")
        items = raw if isinstance(raw, list) else [ids.get("event")]
        out = {str(e["event_id"]) for e in items if isinstance(e, Mapping) and "event_id" in e}
    elif ref_kind == "message":
        raw = ids.get("messages")
        out = {
            str(m["message_id"])
            for m in (raw if isinstance(raw, list) else [])
            if isinstance(m, Mapping) and "message_id" in m
        }
    else:
        doc = ids.get("document")
        out = {str(doc["file_id"])} if isinstance(doc, Mapping) and "file_id" in doc else set()
    return out


def citation(records: Sequence[Record]) -> dict[str, Any]:
    """답이 가리킨 아티팩트 vs 과제 근거 태그(`ref_*`)의 precision·recall.

    비교는 정답 종류가 가리키는 아티팩트 종류(일정·메일·문서)로 한정한다. 근거 태그에는 정답이 아닌
    근거(취소 알림 메일, 이전 버전 문서 등)도 들어 있어 만점 답의 recall도 1보다 작을 수 있다."""
    rows = [r for r in records if _meta(r) is not None]
    if not rows:
        return unavailable("task_meta 기록 없음(결과 스키마 1)")
    hits = cited_n = ref_n = 0
    precisions: list[float] = []
    recalls: list[float] = []
    n = no_answer = 0
    for r in rows:
        meta = _meta(r) or {}
        ref_kind = ANSWER_REF_KIND.get(str(r.get("expected_kind")))
        refs = set((meta.get("refs") or {}).get(ref_kind or "", []))
        if ref_kind is None or not refs:
            continue
        n += 1
        cited = _cited(r, ref_kind)
        if cited is None:
            no_answer += 1
            cited = set()
        inter = len(cited & refs)
        hits += inter
        cited_n += len(cited)
        ref_n += len(refs)
        recalls.append(inter / len(refs))
        if cited:
            precisions.append(inter / len(cited))
    return {
        "available": True,
        "episodes": n,
        "episodes_without_structured_answer": no_answer,
        "precision": _rate(hits, cited_n),
        "recall": _rate(hits, ref_n),
        "episode_mean_precision": _mean(precisions),
        "episode_mean_recall": _mean(recalls),
        "note": "근거 태그는 정답 외 근거도 포함하므로 recall 상한이 1보다 작을 수 있다",
    }


def abstention(records: Sequence[Record]) -> dict[str, Any]:
    """구조화된 답의 kind로 센다. 문장으로만 기권한 경우는 세지 못한다."""
    unans = [r for r in _scored(records) if r.get("expected_kind") == "abstain"]
    answerable = [r for r in _scored(records) if r.get("expected_kind") in ANSWERING_KINDS]
    abstained = sum(_answer_kind(r) == "abstain" for r in unans)
    answered = sum(_answer_kind(r) in ANSWERING_KINDS for r in unans)
    refused = sum(_answer_kind(r) == "abstain" for r in answerable)
    return {
        "available": True,
        "unanswerable_episodes": len(unans),
        "abstain_rate": _rate(abstained, len(unans)),
        "hallucination_rate": _rate(answered, len(unans)),
        "other_rate": _rate(len(unans) - abstained - answered, len(unans)),
        "answerable_episodes": len(answerable),
        "over_refusal_rate": _rate(refused, len(answerable)),
        "note": "되묻기(clarify)·답 없음은 기권도 환각도 아니라 other로 센다",
    }


# ---------------------------------------------------------------- 섹션


def accuracy_extra(records: Sequence[Record]) -> dict[str, Any]:
    writes = [r for r in records if isinstance(r.get("change_matches"), list)]
    if not writes:
        write_fields: dict[str, Any] = unavailable("쓰기 과제 없음")
    else:
        rows = [r for r in writes if isinstance(r.get("change_fields"), list)]
        if not rows:
            write_fields = unavailable("change_fields 기록 없음(결과 스키마 1)")
        else:
            matched: Counter[str] = Counter()
            total: Counter[str] = Counter()
            for r in rows:
                for change in r["change_fields"]:
                    for name, ok in (change.get("fields") or {}).items():
                        total[name] += 1
                        matched[name] += bool(ok)
            write_fields = {
                "available": True,
                "episodes": len(rows),
                "by_field": {
                    k: {
                        "matched": matched[k],
                        "total": total[k],
                        "rate": _rate(matched[k], total[k]),
                    }
                    for k in sorted(total)
                },
            }
    multi = [r for r in _scored(records) if (_meta(r) or {}).get("type") == "multi_turn"]
    if not any(_meta(r) for r in records):
        multi_turn: dict[str, Any] = unavailable("task_meta 기록 없음(결과 스키마 1)")
    else:
        ok = sum(_success(r) for r in multi)
        multi_turn = {
            "available": True,
            "episodes": len(multi),
            "success_rate": _rate(ok, len(multi)),
            "note": MULTI_TURN_NOTE,
        }
    return {"write_fields": write_fields, "multi_turn": multi_turn}


def grounding(records: Sequence[Record]) -> dict[str, Any]:
    return {
        "fact_support": fact_support(records),
        "citation": citation(records),
        "abstention": abstention(records),
    }


def _tool_selection(rows: Sequence[Record]) -> dict[str, Any]:
    hit = need = calls = wrong = 0
    per_area_hit: Counter[str] = Counter()
    per_area_need: Counter[str] = Counter()
    macro: list[float] = []
    n = 0
    for r in rows:
        meta = _meta(r)
        if meta is None:
            continue
        expected = expected_areas(meta)
        if not expected:
            continue
        n += 1
        used = {TOOL_AREA[c["name"]] for c in _counted_calls(r) if c.get("name") in TOOL_AREA}
        got = expected & used
        hit += len(got)
        need += len(expected)
        macro.append(len(got) / len(expected))
        for area in expected:
            per_area_need[area] += 1
            per_area_hit[area] += area in got
        for c in _counted_calls(r):
            call_area = TOOL_AREA.get(str(c.get("name")))
            if call_area is not None:
                calls += 1
                wrong += call_area not in expected
    return {
        "available": True,
        "episodes": n,
        "recall": _rate(hit, need),
        "episode_mean_recall": _mean(macro),
        "recall_by_area": {
            a: {
                "hit": per_area_hit[a],
                "need": per_area_need[a],
                "rate": _rate(per_area_hit[a], per_area_need[a]),
            }
            for a in sorted(per_area_need)
        },
        "wrong_area_call_rate": _rate(wrong, calls),
        "calls_counted": calls,
        "note": "기대 영역은 과제 근거 태그와 유형·시나리오 표에서 정한다(`TOOL_AREA` 등). "
        "쓰기 과제는 대상을 찾으려 일정·메일을 조회하는데, 기대 영역에 그 데이터 영역이 "
        "없으면 이 조회가 잘못된 영역 호출로 센다. 해석에 주의한다",
    }


def _argument_validity(rows: Sequence[Record]) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    total = invalid = 0
    for r in rows:
        for c in _tool_calls(r):
            total += 1
            kind = c.get("error_kind")
            if isinstance(kind, str) and kind in INVALID_ARGUMENT_KINDS:
                invalid += 1
                kinds[kind] += 1
    return {
        "available": True,
        "tool_calls": total,
        "invalid_calls": invalid,
        "validity_rate": None if total == 0 else 1 - invalid / total,
        "invalid_by_kind": dict(sorted(kinds.items())),
    }


def _excess_calls(rows: Sequence[Record]) -> dict[str, Any]:
    used: list[int] = []
    excess: list[int] = []
    minimum: list[int] = []
    for r in rows:
        meta = _meta(r)
        floor = r.get("min_tool_calls")
        if meta is None or not isinstance(floor, int) or meta.get("derived") == "fault":
            continue  # 결함 과제는 재시도가 정상이라 뺀다
        n = len(_counted_calls(r))
        used.append(n)
        minimum.append(floor)
        excess.append(max(0, n - floor))
    if not used:
        return unavailable("최소 호출 수(min_tool_calls) 기록 없음")
    return {
        "available": True,
        "episodes": len(used),
        "mean_calls": _mean(used),
        "mean_min_calls": _mean(minimum),
        "mean_excess_calls": _mean(excess),
        "share_with_excess": _rate(sum(e > 0 for e in excess), len(excess)),
        "note": "최소 호출 수는 정답을 아는 오라클의 호출 수(하한)이고 결함 과제는 제외한다",
    }


def tools(records: Sequence[Record]) -> dict[str, Any]:
    rows = [r for r in records if _trace(r) is not None]
    if not rows:
        reason = _NO_TRACE
        return {
            "selection": unavailable(reason),
            "argument_validity": unavailable(reason),
            "excess_calls": unavailable(reason),
        }
    return {
        "selection": _tool_selection(rows),
        "argument_validity": _argument_validity(rows),
        "excess_calls": _excess_calls(rows),
    }


def mentions_failure(text: str, structured_kind: str | None) -> bool:
    """답이 실패를 알렸는가: 구조화된 답이 기권이거나 문장에 `FAILURE_PHRASES`가 있다."""
    if structured_kind == "abstain":
        return True
    norm = normalize_text(text)
    return any(normalize_text(p) in norm for p in FAILURE_PHRASES)


def fault_fired(r: Record) -> bool | None:
    """호출 기록에 주입된 장애가 있는가. 장애 기록 필드가 없는 옛 결과는 None."""
    calls = [c for c in r.get("calls") or [] if isinstance(c, Mapping)]
    if not any("fault" in c for c in calls):
        return None
    return any(c.get("fault") for c in calls)


def _fault_section(records: Sequence[Record]) -> dict[str, Any]:
    scored = _scored(records)
    fault_rows = [r for r in scored if r.get("task_fault")]
    other = [r for r in scored if "task_fault" in r and not r.get("task_fault")]
    if not fault_rows:
        return unavailable("결함 주입 과제 없음")
    ok = sum(_success(r) for r in fault_rows)
    section: dict[str, Any] = {
        "available": True,
        "episodes": len(fault_rows),
        "success_rate": _rate(ok, len(fault_rows)),
        "non_fault_success_rate": _rate(sum(_success(r) for r in other), len(other)),
    }
    traced = [r for r in fault_rows if _trace(r) is not None and fault_fired(r) is not None]
    if not traced:
        section["honest_report_rate"] = unavailable(_NO_TRACE)
        section["hallucination_rate"] = unavailable(_NO_TRACE)
        return section
    failed = [r for r in traced if fault_fired(r) and not _success(r)]
    honest = halluc = 0
    for r in failed:
        text = str((_trace(r) or {}).get("final_text") or "")
        honest += mentions_failure(text, _answer_kind(r))
        facts = answer_facts(r) or []
        halluc += any(not hit for _, hit in facts)
    section["failed_after_fault_episodes"] = len(failed)
    section["honest_report_rate"] = {
        "available": True,
        "n": len(failed),
        "rate": _rate(honest, len(failed)),
        "note": "표현 목록 기반이라 목록에 없는 실패 표현은 놓친다",
    }
    section["hallucination_rate"] = {
        "available": True,
        "n": len(failed),
        "rate": _rate(halluc, len(failed)),
        "note": "답 문장에 근거 없는 날짜·시각·금액·숫자·이메일·이름이 하나라도 있으면 환각",
    }
    return section


def answer_signature(r: Record) -> str | None:
    """구조화된 답 요약(`answer_ids`)의 순서 없는 서명. 답이 없으면 None."""
    ids = r.get("answer_ids")
    if not isinstance(ids, Mapping):
        return None
    import json

    def norm(value: Any) -> Any:
        if isinstance(value, list):
            return sorted((norm(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
        if isinstance(value, Mapping):
            return {k: norm(v) for k, v in value.items()}
        return value

    return json.dumps(norm(dict(ids)), sort_keys=True, ensure_ascii=False)


def _paraphrase_section(records: Sequence[Record]) -> dict[str, Any]:
    if not any(_meta(r) for r in records):
        return unavailable("task_meta 기록 없음(결과 스키마 1)")
    by_key = {(str(r["task_id"]), int(r.get("repeat", 0))): r for r in _scored(records)}
    pairs: list[tuple[Record, Record]] = []
    for r in by_key.values():
        meta = _meta(r) or {}
        if meta.get("derived") != "paraphrase" or not meta.get("origin_id"):
            continue
        origin = by_key.get((str(meta["origin_id"]), int(r.get("repeat", 0))))
        if origin is not None:
            pairs.append((origin, r))
    if not pairs:
        return unavailable("원본·변형 짝 없음")
    agree = sum(_success(a) == _success(b) for a, b in pairs)
    orig_rate = sum(_success(a) for a, _ in pairs) / len(pairs)
    para_rate = sum(_success(b) for _, b in pairs) / len(pairs)
    sigs = [(answer_signature(a), answer_signature(b)) for a, b in pairs]
    both = [(x, y) for x, y in sigs if x is not None and y is not None]
    return {
        "available": True,
        "pairs": len(pairs),
        "success_agreement": agree / len(pairs),
        "original_success_rate": orig_rate,
        "paraphrase_success_rate": para_rate,
        "accuracy_gap_original_minus_paraphrase": orig_rate - para_rate,
        "answer_agreement": _rate(sum(x == y for x, y in both), len(both)),
        "answer_agreement_pairs": len(both),
        "note": "짝은 (원본 과제, 변형 과제, 같은 반복 번호)이고 구조화된 답 요약이 같으면 답 일치",
    }


def robustness(records: Sequence[Record]) -> dict[str, Any]:
    return {"fault": _fault_section(records), "paraphrase": _paraphrase_section(records)}


def efficiency(records: Sequence[Record]) -> dict[str, Any]:
    scored = _scored(records)
    known = [r for r in scored if (r.get("usage") or {}).get("cost_usd") is not None]
    successes = sum(_success(r) for r in known)
    cost = sum(float(r["usage"]["cost_usd"]) for r in known)
    latencies = [
        float(r["latency_ms"]) for r in records if isinstance(r.get("latency_ms"), int | float)
    ]
    traced = [r for r in records if _trace(r) is not None]
    return {
        "cost_per_success_usd": _rate_float(cost, successes),
        "cost_total_usd": cost if known else None,
        "episodes_with_cost": len(known),
        "episodes_cost_unknown": len(scored) - len(known),
        "successes_with_cost": successes,
        "latency_ms_p50": percentile(latencies, 0.5),
        "latency_ms_p95": percentile(latencies, 0.95),
        "tool_calls_per_episode": (
            _mean([len(_counted_calls(r)) for r in traced]) if traced else unavailable(_NO_TRACE)
        ),
        "connector_calls_per_episode": _mean([len(r.get("calls") or []) for r in records]),
        "note": "지연은 에피소드 전체 시간이다(첫 토큰 시간은 재지 않는다)",
    }


def _rate_float(num: float, den: int) -> float | None:
    return num / den if den else None


def extra_metrics(records: Sequence[Record]) -> dict[str, Any]:
    """`stats` 출력에 더하는 섹션 전체."""
    return {
        "accuracy_extra": accuracy_extra(records),
        "grounding": grounding(records),
        "tools": tools(records),
        "robustness": robustness(records),
        "efficiency": efficiency(records),
    }


# ---------------------------------------------------------------- Judge 입력


def judge_inputs(records: Sequence[Record]) -> Iterator[dict[str, Any]]:
    """서술형 근거성 Judge용 입력(ADR-0029 §3). 과제 ID·반복 번호, 답 문장, 도구 결과 본문만 담는다.
    Judge 호출은 여기서 하지 않는다. `trace`가 없거나 답 문장이 없는 행은 건너뛴다."""
    for r in records:
        trace = _trace(r)
        text = (trace or {}).get("final_text")
        if trace is None or not isinstance(text, str) or not text.strip():
            continue
        yield {
            "task_id": r["task_id"],
            "repeat": r.get("repeat"),
            "answer_text": text,
            "tool_results": [
                {"name": c.get("name"), "result": c.get("result")}
                for c in _tool_calls(r)
                if c.get("name") not in IGNORED_TOOLS and not c.get("is_error")
            ],
        }
