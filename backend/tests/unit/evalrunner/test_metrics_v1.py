"""보조 지표 v1(ADR-0029 §2): 정답 사례·경계 사례·옛 결과 호환. 결과 행은 모두 합성 값이다."""

import json
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner import metrics_v1 as m
from orbit.evalrunner.cli import main as cli_main


def tool(name: str, result: str = "{}", *, error: str | None = None, **args: Any) -> dict[str, Any]:
    return {
        "turn": None,
        "name": name,
        "arguments": args,
        "result": result,
        "result_chars": len(result),
        "result_truncated": False,
        "is_error": error is not None,
        "error_kind": error,
    }


def row(
    task_id: str = "t-1",
    *,
    success: bool | None = True,
    repeat: int = 1,
    expected_kind: str | None = None,
    answer: dict[str, Any] | None = None,
    text: str | None = "답입니다",
    tools_: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    old: bool = False,
    **over: Any,
) -> dict[str, Any]:
    r: dict[str, Any] = {
        "task_id": task_id,
        "repeat": repeat,
        "outcome": {"success": success, "failure_type": None if success else "wrong_answer"},
        "expected_kind": expected_kind,
        "answer_ids": answer,
        "calls": [],
        "usage": {"cost_usd": 0.01},
        "latency_ms": 1000,
        "task_fault": None,
    }
    if not old:
        r["result_schema"] = 2
        r["task_meta"] = {
            "type": "calendar",
            "scenario": "day_agenda",
            "derived": "none",
            "origin_id": None,
            "refs": {"event": ["ev1", "ev2"], "message": [], "file": []},
            "as_of": "2026-11-16T09:00:00+09:00",
            "n_turns": 1,
            **(meta or {}),
        }
        r["trace"] = {
            "turns": [{"user": "질문", "assistant": text, "structured": answer}],
            "tool_calls": tools_ or [],
            "final_text": text,
            "structured": answer,
            "answer_handles": {},
        }
        r["min_tool_calls"] = 1
    r.update(over)
    return r


# ------------------------------------------------------------------ 기대 도구 영역


def meta_of(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"type": "calendar", "scenario": "x", "refs": {}}
    return {**base, **kw}


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (meta_of(refs={"event": ["e"]}), {"calendar"}),
        (meta_of(type="mail", refs={"message": ["m"]}), {"mail"}),
        (meta_of(type="calendar", refs={"event": ["e"], "message": ["m"]}), {"calendar", "mail"}),
        (meta_of(type="document", refs={"file": ["f"]}), {"document"}),
        (meta_of(type="write", refs={"message": ["m"]}), {"mail", "write"}),
        (meta_of(type="reminder", refs={"file": ["f"]}), {"document", "reminder"}),
        (
            meta_of(type="multi_turn", scenario="mail_fact", refs={"message": ["m"]}),
            {"mail", "write"},
        ),
        (meta_of(type="multi_turn", scenario="day_agenda", refs={"event": ["e"]}), {"calendar"}),
        (meta_of(type="ambiguous", refs={"event": ["e"]}), set()),
    ],
)
def test_expected_areas_table(meta: dict[str, Any], expected: set[str]) -> None:
    assert m.expected_areas(meta) == frozenset(expected)


def test_every_known_tool_has_an_area_and_ignored_tools_have_none() -> None:
    assert set(m.TOOL_AREA.values()) == {"calendar", "mail", "document", "write", "reminder"}
    assert not set(m.TOOL_AREA) & m.IGNORED_TOOLS


# ------------------------------------------------------------------ 도구 지표


def test_tool_selection_recall_and_wrong_area_rate() -> None:
    meta = {
        "type": "calendar",
        "scenario": "cancellation",
        "refs": {"event": ["e"], "message": ["m"]},
    }
    full = row(
        "a", tools_=[tool("list_events"), tool("search_mail"), tool("submit_answer")], meta=meta
    )
    half = row("b", tools_=[tool("list_events"), tool("search_docs")], meta=meta)
    out = m.tools([full, half])["selection"]
    assert out["episodes"] == 2
    assert out["recall"] == pytest.approx(3 / 4)
    assert out["recall_by_area"]["mail"] == {"hit": 1, "need": 2, "rate": 0.5}
    # 선택 호출 4개(list_events, search_mail, list_events, search_docs) 중 문서 도구 1개가 영역 밖
    assert out["wrong_area_call_rate"] == pytest.approx(1 / 4)


def test_tool_selection_skips_episodes_without_expected_tools() -> None:
    r = row(meta={"type": "ambiguous"}, tools_=[tool("list_events")])
    out = m.tools([r])["selection"]
    assert out["episodes"] == 0 and out["recall"] is None


def test_argument_validity_counts_only_argument_errors() -> None:
    r = row(
        tools_=[
            tool("list_events"),
            tool("list_events", '{"error": "invalid_time"}', error="invalid_time"),
            tool("get_event", '{"error": "unknown_handle"}', error="unknown_handle"),
            tool("search_mail", '{"error": "transient_error"}', error="transient_error"),
        ]
    )
    out = m.tools([r])["argument_validity"]
    assert out["tool_calls"] == 4 and out["invalid_calls"] == 2
    assert out["validity_rate"] == pytest.approx(0.5)
    assert out["invalid_by_kind"] == {"invalid_time": 1, "unknown_handle": 1}


def test_excess_calls_uses_floor_and_skips_fault_tasks() -> None:
    plain = row("a", tools_=[tool("list_events")] * 3, min_tool_calls=1)
    exact = row("b", tools_=[tool("list_events")], min_tool_calls=1)
    fault = row("c", tools_=[tool("list_events")] * 3, min_tool_calls=1, meta={"derived": "fault"})
    out = m.tools([plain, exact, fault])["excess_calls"]
    assert out["episodes"] == 2
    assert out["mean_excess_calls"] == pytest.approx(1.0)
    assert out["share_with_excess"] == pytest.approx(0.5)


def test_excess_calls_unavailable_without_floor() -> None:
    r = row(tools_=[tool("list_events")], min_tool_calls=None)
    assert m.tools([r])["excess_calls"]["available"] is False


# ------------------------------------------------------------------ 근거성


def test_fact_support_counts_grounded_over_all_candidates() -> None:
    result = json.dumps({"start": "2026-11-16T14:30:00+09:00", "title": "점검"}, ensure_ascii=False)
    good = row("a", text="11월 16일 오후 2시 30분입니다", tools_=[tool("list_events", result)])
    bad = row("b", text="11월 17일 오후 2시 30분입니다", tools_=[tool("list_events", result)])
    none = row("c", text="일정이 없습니다", tools_=[])
    out = m.fact_support([good, bad, none])
    assert out["facts"] == 4 and out["grounded"] == 3
    assert out["support_rate"] == pytest.approx(0.75)
    assert out["episodes_with_facts"] == 2 and out["episodes_with_ungrounded_fact"] == 1
    assert out["by_kind"]["date"] == {"facts": 2, "grounded": 1, "rate": 0.5}


def test_fact_support_ignores_error_results_as_sources() -> None:
    err = tool("list_events", '{"error": "11월 16일"}', error="transient_error")
    r = row(text="11월 16일에 있습니다", tools_=[err])
    # 질문·기준 날짜에도 11월 16일이 있어(as_of) 근거가 된다. 오류 본문만으로는 근거가 되지 않는다
    r["task_meta"]["as_of"] = "2026-10-01T09:00:00+09:00"
    assert m.fact_support([r])["grounded"] == 0


def test_citation_precision_recall_by_answer_kind() -> None:
    meta = {"refs": {"event": ["ev1", "ev2", "ev3"], "message": ["m1"], "file": []}}
    perfect_subset = row(
        "a",
        expected_kind="event_set",
        meta=meta,
        answer={"kind": "event_set", "events": [{"calendar_id": "c", "event_id": "ev1"}]},
    )
    extra = row(
        "b",
        expected_kind="event_set",
        meta=meta,
        answer={
            "kind": "event_set",
            "events": [
                {"calendar_id": "c", "event_id": "ev2"},
                {"calendar_id": "c", "event_id": "zzz"},
            ],
        },
    )
    mail = row(
        "c",
        expected_kind="message_set",
        meta=meta,
        answer={"kind": "message_set", "messages": [{"message_id": "m1"}]},
    )
    missing = row("d", expected_kind="message_set", meta=meta, answer=None)
    out = m.citation([perfect_subset, extra, mail, missing])
    assert out["episodes"] == 4 and out["episodes_without_structured_answer"] == 1
    # 일정 3건 중 정답 근거: a 1/1, b 1/2. 메일 c 1/1, d 0
    assert out["precision"] == pytest.approx(3 / 4)
    assert out["recall"] == pytest.approx(3 / 8)


def test_citation_skips_time_slot_and_unanswerable() -> None:
    skip = [
        row("a", expected_kind="time_slot", answer={"kind": "time_slot", "start": "x"}),
        row("b", expected_kind="abstain", answer={"kind": "abstain"}),
    ]
    out = m.citation(skip)
    assert out["episodes"] == 0 and out["precision"] is None


def test_abstention_rates() -> None:
    abstain = {"kind": "abstain"}
    answered = {"kind": "message_set", "messages": []}
    rows = [
        row("a", expected_kind="abstain", answer=abstain),
        row("b", expected_kind="abstain", answer=answered),
        row("c", expected_kind="abstain", answer=None, success=False),
        row("d", expected_kind="abstain", answer={"kind": "clarify"}, success=False),
        row("e", expected_kind="message_set", answer=abstain, success=False),
        row("f", expected_kind="message_set", answer=answered),
        row("g", expected_kind="clarify", answer={"kind": "clarify"}),
    ]
    out = m.abstention(rows)
    assert out["unanswerable_episodes"] == 4
    assert out["abstain_rate"] == pytest.approx(0.25)
    assert out["hallucination_rate"] == pytest.approx(0.25)
    assert out["other_rate"] == pytest.approx(0.5)
    assert out["answerable_episodes"] == 2
    assert out["over_refusal_rate"] == pytest.approx(0.5)


# ------------------------------------------------------------------ 강건성


def fault_row(
    task_id: str, text: str, *, success: bool = False, fired: bool = True, **kw: Any
) -> dict[str, Any]:
    calls = [{"tool": "list_events", "fault": "list_events:transient" if fired else None}]
    return row(
        task_id, success=success, text=text, task_fault=["list_events:transient"], calls=calls, **kw
    )


def test_fault_honest_report_and_hallucination() -> None:
    honest = fault_row("a", "일시적인 오류로 일정을 확인하지 못했습니다")
    quiet = fault_row("b", "11월 17일 오후 4시에 회의가 있습니다")
    abstained = fault_row("c", "정리했습니다", answer={"kind": "abstain"})
    recovered = fault_row("d", "일정은 없습니다", success=True)
    not_fired = fault_row("e", "일정이 없습니다", fired=False)
    out = m.robustness([honest, quiet, abstained, recovered, not_fired])["fault"]
    assert out["episodes"] == 5
    assert out["success_rate"] == pytest.approx(1 / 5)
    assert out["failed_after_fault_episodes"] == 3
    assert out["honest_report_rate"]["rate"] == pytest.approx(2 / 3)
    assert out["hallucination_rate"]["rate"] == pytest.approx(1 / 3)


def test_mentions_failure_phrase_boundaries() -> None:
    assert m.mentions_failure("서버 오류가 났습니다", None)
    assert m.mentions_failure("확인 하지 못했어요", None)  # 공백 차이는 무시
    assert m.mentions_failure("아무 말", "abstain")
    assert not m.mentions_failure("일정은 3시입니다", "event")


def test_fault_section_without_trace_still_reports_success_rate() -> None:
    old = row("a", success=False, old=True, task_fault=["list_events:transient"])
    out = m.robustness([old])["fault"]
    assert out["success_rate"] == 0.0
    assert out["honest_report_rate"]["available"] is False


def test_paraphrase_pair_metrics() -> None:
    def pair(
        base: str, ok_a: bool, ok_b: bool, ids_a: str, ids_b: str, repeat: int = 1
    ) -> list[dict[str, Any]]:
        def ans(i: str) -> dict[str, Any]:
            return {"kind": "event_set", "events": [{"calendar_id": "c", "event_id": i}]}

        return [
            row(base, success=ok_a, answer=ans(ids_a), repeat=repeat),
            row(
                f"{base}-para",
                success=ok_b,
                answer=ans(ids_b),
                repeat=repeat,
                meta={"derived": "paraphrase", "origin_id": base},
            ),
        ]

    rows = pair("t-1", True, True, "e1", "e1") + pair("t-2", True, False, "e1", "e2")
    rows += pair("t-3", False, False, "e1", "e1") + [row("t-solo")]
    out = m.robustness(rows)["paraphrase"]
    assert out["pairs"] == 3
    assert out["success_agreement"] == pytest.approx(2 / 3)
    assert out["accuracy_gap_original_minus_paraphrase"] == pytest.approx(2 / 3 - 1 / 3)
    assert out["answer_agreement"] == pytest.approx(2 / 3)


def test_paraphrase_answer_signature_ignores_order() -> None:
    a = row(answer={"kind": "event_set", "events": [{"event_id": "1"}, {"event_id": "2"}]})
    b = row(answer={"kind": "event_set", "events": [{"event_id": "2"}, {"event_id": "1"}]})
    assert m.answer_signature(a) == m.answer_signature(b)
    assert m.answer_signature(row(answer=None)) is None


def test_paraphrase_pairs_must_share_repeat() -> None:
    rows = [
        row("t-1", repeat=1),
        row("t-1-para", repeat=2, meta={"derived": "paraphrase", "origin_id": "t-1"}),
    ]
    assert m.robustness(rows)["paraphrase"]["available"] is False


# ------------------------------------------------------------------ 정확도·효율


def test_write_field_accuracy_aggregates_by_field() -> None:
    def w(fields: dict[str, bool]) -> dict[str, Any]:
        return row(
            change_matches=[True], change_fields=[{"kind": "event_created", "fields": fields}]
        )

    out = m.accuracy_extra(
        [w({"title": True, "time": False}), w({"title": True, "attendees": True})]
    )
    fields = out["write_fields"]["by_field"]
    assert fields["title"] == {"matched": 2, "total": 2, "rate": 1.0}
    assert fields["time"] == {"matched": 0, "total": 1, "rate": 0.0}
    assert fields["attendees"]["total"] == 1


def test_multi_turn_success_is_conversation_level() -> None:
    rows = [
        row("a", meta={"type": "multi_turn"}),
        row("b", meta={"type": "multi_turn"}, success=False),
        row("c", meta={"type": "multi_turn"}, success=None),
        row("d"),
    ]
    out = m.accuracy_extra(rows)["multi_turn"]
    assert out["episodes"] == 2 and out["success_rate"] == 0.5
    assert "턴 단위 정답이 없는 턴" in out["note"]


def test_efficiency_cost_latency_and_calls() -> None:
    rows = [
        row("a", usage={"cost_usd": 0.02}, latency_ms=100, tools_=[tool("list_events")]),
        row("b", usage={"cost_usd": 0.04}, latency_ms=200, success=False),
        row("c", usage={"cost_usd": None}, latency_ms=300),
        row("d", usage={"cost_usd": 0.06}, latency_ms=1000, tools_=[tool("submit_answer")]),
    ]
    out = m.efficiency(rows)
    # 비용을 아는 3건(실패 포함)의 합 0.12를 그중 성공 2건으로 나눈다
    assert out["cost_per_success_usd"] == pytest.approx(0.06)
    assert out["episodes_cost_unknown"] == 1
    assert out["latency_ms_p50"] == 200 and out["latency_ms_p95"] == 1000
    assert out["tool_calls_per_episode"] == pytest.approx(0.25)  # submit_answer는 세지 않는다


def test_percentile_nearest_rank() -> None:
    assert m.percentile([], 0.5) is None
    assert m.percentile([5], 0.95) == 5
    assert m.percentile([1, 2, 3, 4], 0.5) == 2
    assert m.percentile(list(range(1, 101)), 0.95) == 95


# ------------------------------------------------------------------ 옛 결과 호환


def test_old_rows_report_unavailable_with_reason_not_errors() -> None:
    old = [
        row("a", old=True, expected_kind="abstain", answer={"kind": "abstain"}),
        row("b", old=True, expected_kind="message_set", answer={"kind": "abstain"}, success=False),
    ]
    out = m.extra_metrics(old)
    assert out["grounding"]["fact_support"]["available"] is False
    assert "스키마 1" in out["grounding"]["fact_support"]["reason"]
    assert out["grounding"]["citation"]["available"] is False
    assert out["tools"]["selection"]["available"] is False
    assert out["tools"]["excess_calls"]["available"] is False
    assert out["robustness"]["paraphrase"]["available"] is False
    assert out["accuracy_extra"]["multi_turn"]["available"] is False
    assert out["efficiency"]["tool_calls_per_episode"]["available"] is False
    # 옛 필드(expected_kind, answer_ids, usage, latency_ms)로 재는 지표는 값이 나온다
    assert out["grounding"]["abstention"]["abstain_rate"] == 1.0
    assert out["efficiency"]["latency_ms_p50"] == 1000


def test_mixed_old_and_new_rows_use_only_recorded_rows() -> None:
    r = row("a", text="11월 16일", tools_=[tool("list_events", json.dumps({"d": "2026-11-16"}))])
    old = row("b", old=True)
    assert m.fact_support([r, old])["episodes"] == 1


# ------------------------------------------------------------------ 과제 메타·Judge 입력


class _Task:
    def __init__(self, task_id: str, tags: list[str]) -> None:
        from datetime import datetime

        self.task_id = task_id
        self.tags = tags
        self.as_of_time = datetime.fromisoformat("2026-11-16T09:00:00+09:00")
        self.turns = [object(), object()]


def test_task_meta_reads_tags_and_origin() -> None:
    task = _Task(
        "t-mail-fact-0005-para",
        [
            "type:mail",
            "scenario:mail_fact",
            "derived:paraphrase",
            "ref_message:m2",
            "ref_message:m1",
            "ref_file:f1",
            "variant:attachment",
        ],
    )
    meta = m.task_meta(task)
    assert meta["type"] == "mail" and meta["derived"] == "paraphrase"
    assert meta["origin_id"] == "t-mail-fact-0005"
    assert meta["refs"] == {"event": [], "message": ["m1", "m2"], "file": ["f1"]}
    assert meta["n_turns"] == 2 and meta["as_of"].startswith("2026-11-16T09:00")
    fault = m.task_meta(_Task("t-x-0001-fault", ["derived:fault", "fault:transient"]))
    assert fault["origin_id"] == "t-x-0001" and fault["fault_error"] == "transient"
    assert m.task_meta(_Task("t-x", []))["origin_id"] is None


def test_judge_inputs_have_only_answer_results_and_ids() -> None:
    good = row(
        "a", text="답", tools_=[tool("list_events", '{"x": 1}'), tool("submit_answer", "ok")]
    )
    err = row("b", text="오류", tools_=[tool("search_mail", "{}", error="transient_error")])
    old = row("c", old=True)
    notext = row("d", text=None)
    out = list(m.judge_inputs([good, err, old, notext]))
    assert out[0] == {
        "task_id": "a",
        "repeat": 1,
        "sut": None,
        "task_type": "calendar",
        "cluster_id": None,
        "question": "질문",
        "answer_text": "답",
        "tool_results": [{"name": "list_events", "result": '{"x": 1}'}],
    }
    assert out[1]["tool_results"] == []
    assert len(out) == 2


# ------------------------------------------------------------------ CLI


def write_results(path: Path, rows: list[dict[str, Any]]) -> None:
    cfg = {"repeats": 1, "n_tasks": len(rows), "dataset": "d@1", "split": "dev"}
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            r = {**r, "config": cfg, "config_fingerprint": "f", "cluster_id": f"c-{r['task_id']}"}
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_cli_stats_adds_sections_and_judge_input_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "r.jsonl"
    write_results(results, [row("a", text="답"), row("b", old=True)])
    assert cli_main(["stats", str(results), "--resamples", "50", "--include-incomplete"]) == 0
    report = json.loads(capsys.readouterr().out)
    for key in ("accuracy_extra", "grounding", "tools", "robustness", "efficiency"):
        assert key in report
    out = tmp_path / "judge.jsonl"
    assert cli_main(["judge-input", str(results), "--out", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["task_id"] for x in lines] == ["a"]
    assert cli_main(["judge-input", str(results), "--out", str(out)]) == 2  # 덮어쓰지 않는다


def test_as_of_gives_local_date_only_not_time() -> None:
    r = row(text="오전 9시 11월 16일", tools_=[])
    r["task_meta"]["as_of"] = "2026-11-15T23:30:00+00:00"  # 서울 현지로는 11월 16일 08:30
    r["time_zone"] = "Asia/Seoul"
    srcs = m.episode_sources(r)
    assert "2026-11-16" in srcs and not any("08:30" in s or "23:30" in s for s in srcs)
    out = m.fact_support([r])
    assert out["by_kind"]["date"]["grounded"] == 1
    assert out["by_kind"]["time"] == {"facts": 1, "grounded": 0, "rate": 0.0}


def test_selection_note_warns_about_lookups_in_write_tasks() -> None:
    r = row(meta={"type": "write", "refs": {"message": ["m"]}}, tools_=[tool("list_events")])
    assert "잘못된 영역" in m.tools([r])["selection"]["note"]


def test_cli_warns_when_result_schemas_are_mixed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "mixed.jsonl"
    write_results(results, [row("a"), row("b", old=True)])
    assert cli_main(["stats", str(results), "--resamples", "50", "--include-incomplete"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert any("result_schema" in w for w in report["warnings"])
    only_new = tmp_path / "new.jsonl"
    write_results(only_new, [row("a")])
    cli_main(["stats", str(only_new), "--resamples", "50", "--include-incomplete"])
    assert "warnings" not in json.loads(capsys.readouterr().out)
