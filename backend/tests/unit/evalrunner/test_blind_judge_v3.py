"""v3 배정·유형별 의미 변형·키 격리·코드 기준선의 한계를 합성 데이터로 검증한다."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner import blind_judge as b
from orbit.evalrunner import blind_judge_v3 as v3
from orbit.evalrunner import judge as j


def sources() -> list[j.Episode]:
    return [
        j.Episode(
            f"source/private-source-{i}/1",
            f"private-source-{i}",
            1,
            "source",
            "mail",
            f"c-{i}",
            "회의 날짜를 알려줘.",
            f"합성 기록 {i}: 회의 날짜는 11월 17일입니다.",
            (
                (
                    "read_mail",
                    json.dumps(
                        {
                            "ref": f"기록 {i}",
                            "date": "2026-11-17",
                            "previous_date": "2026-11-16",
                            "location": "나무회의실",
                            "sender": "이서준",
                            "recipient": "김하늘",
                            "budget": "9,100,000원",
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
        )
        for i in range(60)
    ]


def candidate(ep: j.Episode, kind: str, answer: str | None = None) -> dict[str, Any]:
    index = ep.task_id.rsplit("-", 1)[-1]
    text = answer or f"합성 기록 {index}: 회의 날짜는 11월 16일입니다."
    return {
        "source_id": ep.episode_id,
        "kind": kind,
        "answer_text": text,
        "mutation_note": f"KEY-ONLY-NOTE-{index}",
        "reviewed": True,
        "introduced_values": [],
    }


def candidates(
    episodes: list[j.Episode], quotas: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    kinds = [kind for kind, count in (quotas or v3.QUOTAS).items() for _ in range(count)]
    return [candidate(ep, kind) for ep, kind in zip(episodes, kinds, strict=False)]


def generate(episodes: list[j.Episode], rows: list[dict[str, Any]]) -> Any:
    return v3.generate(episodes, {ep.episode_id: True for ep in episodes}, rows)


def test_requested_quotas_seed_and_sixty_unique_answers() -> None:
    episodes = sources()
    rows = candidates(episodes)
    inputs, keys, summary = generate(episodes, rows)
    assert summary["exact_quotas"] is True and summary["actual_quotas"] == v3.QUOTAS
    assert len(inputs) == len(keys) == len({key["source_id"] for key in keys}) == 60
    assert sum(key["mutated"] for key in keys) == 30
    assert len({v3.answer_hash(row["answer_text"]) for row in inputs}) == 60
    assert (inputs, keys, summary) == generate(list(reversed(episodes)), list(reversed(rows)))
    assert summary["shortages"] == summary["supplements"] == {}


def test_normalization_removes_markdown_punctuation_and_whitespace() -> None:
    assert v3.answer_hash("**회의**: １１월 １７일!\n") == v3.answer_hash("회의 11월17일")
    assert v3.answer_hash("회의는 11월 17일") != v3.answer_hash("회의는 11월 18일")


def test_duplicate_original_group_forces_mutation_instead_of_duplicate_answer() -> None:
    episodes = sources()
    episodes[31] = replace(episodes[31], answer="**" + episodes[0].answer + "**")
    inputs, keys, _ = generate(episodes, candidates(episodes))
    selected = {key["source_id"]: key["mutated"] for key in keys}
    assert selected[episodes[0].episode_id] is True
    assert selected[episodes[31].episode_id] is False
    assert len({v3.answer_hash(row["answer_text"]) for row in inputs}) == 60


def test_shortage_is_reported_and_only_non_surface_supplements() -> None:
    episodes = sources()
    actual = {**v3.QUOTAS, "stale_value": 3, "role_swap": 6, "unsupported_addition": 5}
    _, keys, summary = generate(episodes, candidates(episodes, actual))
    assert summary["exact_quotas"] is False and summary["actual_quotas"] == actual
    assert summary["shortages"] == {"stale_value": 2}
    assert summary["supplements"] == {"role_swap": 1, "unsupported_addition": 1}
    assert sum(key["kind"] == "surface" for key in keys) == 6
    assert summary["available_sources"]["stale_value"] == 3


def test_surface_is_never_increased_to_fill_a_shortage() -> None:
    episodes = sources()
    actual = {**v3.QUOTAS, "surface": 7, "stale_value": 4}
    with pytest.raises(b.BlindError, match="surface 최대6"):
        generate(episodes, candidates(episodes, actual))


@pytest.mark.parametrize("kind", ["stale_value", "role_swap"])
@pytest.mark.parametrize(
    "answer",
    [
        "회의 날짜는 11월 19일입니다.",
        "예산은 940만 원입니다.",
        "담당자는 박지훈님입니다.",
        "장소는 별빛회의실입니다.",
    ],
)
def test_stale_and_role_auto_extract_new_facts_even_when_manifest_omits_them(
    kind: str,
    answer: str,
) -> None:
    ep = sources()[0]
    row = candidate(ep, kind, answer)
    # 선언값에 이미 있는 값만 써도 새 사실을 답에서 별도로 찾아 검증한다.
    row["introduced_values"] = [{"kind": "date", "value": "11월 17일"}]
    valid, rejected = v3.validate_candidates([ep], [row])
    assert valid == [] and len(rejected) == 1
    assert "실제 도구 결과에 없다" in rejected[0]["reason"]


def test_stale_and_role_allow_only_real_tool_values_including_literal_text() -> None:
    ep = sources()[0]
    body = ep.tool_results[0][1]
    ep = replace(ep, tool_results=(("read_mail", body + "\n상태: B안 채택"),))
    row = candidate(ep, "role_swap", "김하늘님이 이서준님에게 B안 채택 결과를 보냈습니다.")
    row["introduced_values"] = [
        {"kind": "name", "value": "김하늘"},
        {"kind": "text", "value": "B안 채택"},
    ]
    v3.validate_mutation(ep, v3.V3Mutation.parse(row))
    row["introduced_values"] = [{"kind": "text", "value": "C안 채택"}]
    with pytest.raises(b.BlindError, match="새 text 값"):
        v3.validate_mutation(ep, v3.V3Mutation.parse(row))


def test_v3_key_never_reaches_labeler_projection() -> None:
    episodes = sources()
    inputs, keys, _ = generate(episodes, candidates(episodes))
    html = b.render_html(inputs, "")
    assert "KEY-ONLY-NOTE" not in html and "private-source" not in html
    projected = b.labeler_projection(inputs)
    assert all(set(row) == {"n", "question", "answer", "hints", "tools"} for row in projected)
    assert all("mutation_note" not in row and "introduced_values" not in row for row in projected)
    assert all(key["mutation_note"] for key in keys if key["mutated"])


def test_per_kind_missing_keeps_class_and_total_denominators() -> None:
    episodes = sources()
    _, keys, _ = generate(episodes, candidates(episodes))
    predictions = {key["id"]: not key["mutated"] for key in keys}
    missing = next(key for key in keys if key["kind"] == "role_swap")
    del predictions[missing["id"]]
    report = v3.score_key(keys, predictions, cost_usd=0.125)
    assert report["n"] == 60 and report["accuracy"] == pytest.approx(59 / 60)
    assert report["unsupported_recall"] == pytest.approx(29 / 30)
    assert report["per_kind"]["role_swap"] == {"n": 5, "detected": 4, "recall": 0.8, "missing": 1}
    assert report["non_surface_n"] == 24 and report["non_surface_recall"] == pytest.approx(23 / 24)
    assert report["cost_usd"] == 0.125 and report["human_kappa"] == "pending"


def test_code_baseline_catches_absent_surface_value_but_misses_role_semantics() -> None:
    tool = json.dumps(
        {"sender": "이서준", "recipient": "김하늘", "date": "2026-11-17"}, ensure_ascii=False
    )
    inputs = [
        {
            "sut": "blind",
            "task_id": "surface",
            "repeat": 1,
            "answer_text": "회의 날짜는 11월 18일입니다.",
            "tool_results": [{"name": "read_mail", "result": tool}],
        },
        {
            "sut": "blind",
            "task_id": "role",
            "repeat": 1,
            "answer_text": "김하늘님이 이서준님에게 보냈습니다.",
            "tool_results": [{"name": "read_mail", "result": tool}],
        },
    ]
    predictions = v3.baseline_predictions(inputs)
    assert predictions == {"blind/surface/1": False, "blind/role/1": True}


def test_candidate_rejection_is_reported_with_type_and_source() -> None:
    episodes = sources()
    rows = candidates(episodes)
    bad = candidate(episodes[0], "stale_value", "회의는 11월 21일입니다.")
    _, _, summary = generate(episodes, [*rows, bad])
    assert summary["rejected_candidates"] == [
        {
            "source_id": episodes[0].episode_id,
            "kind": "stale_value",
            "reason": "새 date 값이 실제 도구 결과에 없다",
        }
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "마감일을 확인하지 못했어요.",
        "금액을 문안은 최근에 변경해 조건의 의미를 살펴봤어요.",
    ],
)
def test_baseline_does_not_guess_people_from_ordinary_nouns(answer: str) -> None:
    row = {
        "sut": "blind",
        "task_id": "ordinary-nouns",
        "repeat": 1,
        "answer_text": answer,
        "tool_results": [{"name": "read_mail", "result": "조회 결과 없음"}],
    }
    assert v3.baseline_predictions([row]) == {"blind/ordinary-nouns/1": True}


@pytest.mark.parametrize("place", ["회의실 3층B", "본관 2층 세미나실"])
def test_baseline_recognizes_place_phrase_without_label_or_markdown(place: str) -> None:
    row = {
        "sut": "blind",
        "task_id": "supported-place",
        "repeat": 1,
        "answer_text": f"장소는 **{place}**입니다.",
        "tool_results": [
            {"name": "get_event", "result": json.dumps({"location": place}, ensure_ascii=False)}
        ],
    }
    assert v3.baseline_predictions([row]) == {"blind/supported-place/1": True}


def test_baseline_strips_markdown_before_name_extraction_and_checks_supported_name() -> None:
    row = {
        "sut": "blind",
        "task_id": "supported-name",
        "repeat": 1,
        "answer_text": "**이서준**님이 요청했습니다.",
        "tool_results": [{"name": "read_mail", "result": "요청자: 이서준"}],
    }
    assert v3.baseline_predictions([row])["blind/supported-name/1"] is True
    row["answer_text"] = "**박지훈**님이 요청했습니다."
    assert v3.baseline_predictions([row])["blind/supported-name/1"] is False


def test_baseline_omits_general_numbers_and_email_from_its_declared_scope() -> None:
    row = {
        "sut": "blind",
        "task_id": "scope",
        "repeat": 1,
        "answer_text": "정리 번호는 8675309이고 주소는 synthetic@example.net입니다.",
        "tool_results": [{"name": "read_mail", "result": "{}"}],
    }
    assert v3.baseline_predictions([row]) == {"blind/scope/1": True}


@pytest.mark.parametrize("ending", ["입니다", "이다", "이며", "이에요"])
def test_explicit_name_label_recognizes_sentence_endings_and_cannot_hide_new_name(
    ending: str,
) -> None:
    ep = sources()[0]
    supported = candidate(ep, "role_swap", f"담당자는 김하늘{ending}.")
    v3.validate_mutation(ep, v3.V3Mutation.parse(supported))
    unsupported = candidate(ep, "role_swap", f"담당자는 박지훈{ending}.")
    assert unsupported["introduced_values"] == []
    with pytest.raises(b.BlindError, match="새 name 값이 실제 도구 결과에 없다"):
        v3.validate_mutation(ep, v3.V3Mutation.parse(unsupported))
    row = {
        "sut": "blind",
        "task_id": "name-ending",
        "repeat": 1,
        "answer_text": supported["answer_text"],
        "tool_results": [{"name": name, "result": body} for name, body in ep.tool_results],
    }
    assert v3.baseline_predictions([row])["blind/name-ending/1"] is True
    row["answer_text"] = unsupported["answer_text"]
    assert v3.baseline_predictions([row])["blind/name-ending/1"] is False


@pytest.mark.parametrize("tool_time,allowed", [("14:00", False), ("14:30", True)])
def test_half_hour_uses_same_parser_for_extraction_and_source_validation(
    tool_time: str,
    allowed: bool,
) -> None:
    ep = replace(
        sources()[0],
        answer="회의는 오후 1시입니다.",
        tool_results=(("get_event", json.dumps({"start": "13:00", "other_start": tool_time})),),
    )
    row = candidate(ep, "stale_value", "회의는 오후 2시 반입니다.")
    valid, rejected = v3.validate_candidates([ep], [row])
    assert bool(valid) is allowed and bool(rejected) is not allowed
    if rejected:
        assert rejected[0]["reason"] == "새 time 값이 실제 도구 결과에 없다"


@pytest.mark.parametrize("tool_date,allowed", [("11/18", False), ("11/19", True)])
def test_slash_date_uses_same_parser_for_extraction_and_source_validation(
    tool_date: str,
    allowed: bool,
) -> None:
    ep = replace(
        sources()[0],
        tool_results=(("read_mail", json.dumps({"date": "11/17", "other_date": tool_date})),),
    )
    row = candidate(ep, "stale_value", "회의 날짜는 11/19입니다.")
    valid, rejected = v3.validate_candidates([ep], [row])
    assert bool(valid) is allowed and bool(rejected) is not allowed
    if rejected:
        assert rejected[0]["reason"] == "새 date 값이 실제 도구 결과에 없다"


def test_compound_amount_uses_existing_fact_normalization() -> None:
    ep = replace(sources()[0], tool_results=(("read_mail", "예산은 120,000,000원입니다."),))
    row = candidate(ep, "role_swap", "예산은 1억 2천만 원입니다.")
    v3.validate_mutation(ep, v3.V3Mutation.parse(row))
    ep = replace(ep, tool_results=(("read_mail", "예산은 100,000,000원입니다."),))
    with pytest.raises(b.BlindError, match="새 money 값이 실제 도구 결과에 없다"):
        v3.validate_mutation(ep, v3.V3Mutation.parse(row))


def test_mutation_requires_explicit_year_while_baseline_keeps_its_documented_limitation() -> None:
    ep = replace(sources()[0], answer="회의는 2026년 11월 17일입니다.")
    row = candidate(ep, "stale_value", "회의는 2027년 11월 17일입니다.")
    assert row["introduced_values"] == []
    with pytest.raises(b.BlindError, match="새 date 값이 실제 도구 결과에 없다"):
        v3.validate_mutation(ep, v3.V3Mutation.parse(row))
    original_input = {
        "sut": "blind",
        "task_id": "year-limit",
        "repeat": 1,
        "answer_text": row["answer_text"],
        "tool_results": [{"name": name, "result": body} for name, body in ep.tool_results],
    }
    assert v3.baseline_predictions([original_input]) == {"blind/year-limit/1": True}


def test_python_module_cli_returns_usage_error_without_traceback_for_invalid_candidates(
    tmp_path: Path,
) -> None:
    episodes = sources()
    inputs = [
        {
            "sut": ep.sut,
            "task_id": ep.task_id,
            "repeat": ep.repeat,
            "question": ep.question,
            "answer_text": ep.answer,
            "tool_results": [{"name": name, "result": body} for name, body in ep.tool_results],
        }
        for ep in episodes
    ]
    labels = [{"id": ep.episode_id, "grounded": True} for ep in episodes]
    # 29개 변조로는 요구하는 30개를 배정할 수 없어 v3 모듈의 BlindError가 발생한다.
    paths = {}
    for name, rows in (
        ("input", inputs),
        ("human", labels),
        ("candidates", candidates(episodes)[:-1]),
    ):
        path = tmp_path / f"{name}.jsonl"
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), "utf-8"
        )
        paths[name] = path
    result = subprocess.run(  # noqa: S603  이 테스트의 Python/고정 모듈과 임시 합성 파일만 실행한다.
        [
            sys.executable,
            "-m",
            "orbit.evalrunner.blind_judge",
            "build-v3",
            "--input",
            str(paths["input"]),
            "--human",
            str(paths["human"]),
            "--candidates",
            str(paths["candidates"]),
            "--out",
            str(tmp_path / "never-written"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 2 and "배정 불가" in result.stderr
    assert "Traceback" not in result.stderr and not (tmp_path / "never-written").exists()
