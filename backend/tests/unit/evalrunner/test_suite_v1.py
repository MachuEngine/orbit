"""스위트 v1 측정 도구(ADR-0026 §4·§7): 새 정답 유형 채점, 실패 유형, 도구 인자 기록, 보고 비율.

`assistant_v0`를 `ScriptedLLM`으로 끝까지 돌린다. 데이터는 모두 합성 값이다.
"""

import json
import shutil
from pathlib import Path
from typing import Any

from orbit.connectors.sim.calls import CallRecord
from orbit.connectors.sim.world import WorldDiff
from orbit.evaldata.schema import Gold
from orbit.evalrunner import stats
from orbit.evalrunner.runner import ExperimentSummary
from orbit.evalrunner.scoring import score_episode
from orbit.evalrunner.task_source import Dataset, load_dataset

from .conftest import DATASET_DIR, call, run, step

MAIN = "cal-alice-main"
EV1 = {"calendar_id": MAIN, "event_id": "evalice001"}
EV2 = {"calendar_id": MAIN, "event_id": "evalice002"}
BOB_MAIL = {
    "kind": "message_set",
    "messages": [{"message_id": "msg-a-001"}, {"message_id": "msg-a-003"}],
}
LIST_DAY = call(
    "list_events", time_min="2026-11-12T00:00:00+09:00", time_max="2026-11-13T00:00:00+09:00"
)
SEARCH_BOB = call("search_mail", query="from:bob@example.org")

EXPECTED: dict[str, dict[str, Any]] = {
    "v-mail": BOB_MAIL,
    "v-mail-kind": {"kind": "event_set", "events": [EV1]},  # 한 일정이 정답인 목록 질문
    "v-abstain": {"kind": "abstain"},
    "v-clarify": {
        "kind": "clarify",
        "readings": [
            {"kind": "event", "event": EV1},
            {"kind": "event_set", "events": [EV1, EV2]},
        ],
    },
    "v-clarify-b": {
        "kind": "clarify",
        "readings": [
            {"kind": "event", "event": EV1},
            {"kind": "event_set", "events": [EV1, EV2]},
        ],
    },
    "v-plain": {"kind": "event", "event": EV1},
}

SUBMITS: dict[str, dict[str, Any]] = {
    "v-mail": {"kind": "message_set", "messages": ["M2", "M1"]},
    "v-mail-kind": {"kind": "event", "event": "E1"},  # 대상은 같고 종류만 틀렸다
    "v-abstain": {"kind": "abstain"},
    "v-clarify": {"kind": "clarify"},
    "v-clarify-b": {"kind": "event_set", "events": ["E1", "E2"]},  # 해석 2번으로만 답했다
    "v-plain": {"kind": "clarify"},  # 한 가지로 읽히는데 되물었다
}


def make_dataset(tmp_path: Path) -> Dataset:
    root = tmp_path / "v1-ds"
    root.mkdir()
    shutil.copy(DATASET_DIR / "world.json", root / "world.json")
    manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["counts"]["tasks"] = len(EXPECTED)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    rows = []
    for task_id, expected in EXPECTED.items():
        rows.append(
            {
                "task_id": task_id,
                "scenario_id": f"s-{task_id}",
                "cluster_id": f"c-{task_id}",
                "split": "dev",
                "as_of_time": "2026-11-11T09:00:00+09:00",
                "account_id": "acct-alice",
                "turns": [{"role": "user", "content": f"합성 질문 {task_id}"}],
                "gold": {
                    "answer_facts": [],
                    "must_not_contain": [],
                    "expected_changes": [],
                    "required_tool_calls": [],
                    "forbidden_tools": ["create_event", "update_event", "cancel_event"],
                    "expected_answer": expected,
                },
                "difficulty": {"level": 1},
                "tags": ["mail", "read"],
            }
        )
    (root / "tasks.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return load_dataset(root)


def script() -> dict[str, list[dict[str, Any]]]:
    first = {"v-mail": SEARCH_BOB}
    return {
        tid: [
            step(calls=[first.get(tid, LIST_DAY)]),
            {**step("짧은 답"), "structured": sub},
        ]
        for tid, sub in SUBMITS.items()
    }


def outcome(summary: ExperimentSummary) -> dict[str, tuple[bool | None, str | None]]:
    return {r.task_id: (r.success, r.failure_type) for r in summary.results}


# ------------------------------------------------------------ 두 평가 대상 종단


async def test_message_set_and_clarify_are_scored_end_to_end(tmp_path: Path) -> None:
    summary = await run(make_dataset(tmp_path), script(), sut="assistant_v0", repeats=1)
    assert outcome(summary) == {
        "v-mail": (True, None),
        "v-mail-kind": (False, "answer_kind"),
        "v-abstain": (True, None),
        "v-clarify": (True, None),
        "v-clarify-b": (False, "ambiguity_ignored"),
        "v-plain": (False, "unneeded_clarify"),
    }
    rows = {r.task_id: r.to_dict() for r in summary.results}
    assert rows["v-clarify-b"]["matched_reading"] == 1
    assert rows["v-mail"]["matched_reading"] is None
    got = rows["v-mail"]["answer_ids"]["messages"]
    assert {m["message_id"] for m in got} == {"msg-a-001", "msg-a-003"}
    assert rows["v-clarify"]["expected_kind"] == "clarify"
    assert rows["v-mail"]["expected_kind"] == "message_set"


async def test_result_rows_carry_calls_with_summarized_args(tmp_path: Path) -> None:
    summary = await run(make_dataset(tmp_path), script(), sut="assistant_v0", repeats=1)
    row = next(r.to_dict() for r in summary.results if r.task_id == "v-mail")
    assert row["trajectory"] == ["search_messages"]  # 호환용 기존 필드
    (entry,) = row["calls"]
    assert set(entry) == {"tool", "args", "outcome", "error_type"}  # result_summary는 없다
    assert entry["tool"] == "search_messages" and entry["outcome"] == "ok"
    assert entry["error_type"] is None
    assert entry["args"]["query"] == "from:bob@example.org"
    json.dumps(row)  # JSON 직렬화 가능


async def test_calls_args_are_length_marked_for_long_strings(tmp_path: Path) -> None:
    long_query = "가" * 100
    scr = script()
    scr["v-mail"][0] = step(calls=[call("search_mail", query=long_query)])
    summary = await run(make_dataset(tmp_path), scr, sut="assistant_v0", repeats=1)
    row = next(r.to_dict() for r in summary.results if r.task_id == "v-mail")
    assert row["calls"][0]["args"]["query"] == "<str len=100>"
    assert long_query not in json.dumps(row, ensure_ascii=False)


async def test_report_rates_and_reading_counts(tmp_path: Path) -> None:
    summary = await run(make_dataset(tmp_path), script(), sut="assistant_v0", repeats=1)
    rows = [r.to_dict() for r in summary.results]
    assert stats.failure_distribution(rows) == {
        "ambiguity_ignored": 1,
        "answer_kind": 1,
        "unneeded_clarify": 1,
    }
    assert stats.answer_kind_rate(rows) == 1 / 6
    # 정답이 clarify가 아닌 4문항 중 1문항이 과잉 되묻기
    assert stats.unneeded_clarify_rate(rows) == 1 / 4
    assert stats.ambiguity_ignored_by_reading(rows) == {"1": 1}


def _row(
    failure: str | None, *, expected: str | None, reading: int | None = None
) -> dict[str, Any]:
    return {
        "outcome": {"success": failure is None, "failure_type": failure},
        "expected_kind": expected,
        "matched_reading": reading,
    }


def test_rates_handle_empty_and_old_rows() -> None:
    assert stats.answer_kind_rate([]) is None
    assert stats.unneeded_clarify_rate([]) is None
    assert stats.ambiguity_ignored_by_reading([_row(None, expected="event")]) is None
    old = {"outcome": {"success": False, "failure_type": "answer_format"}}  # v0 이전 행
    assert stats.unneeded_clarify_rate([old]) == 0.0
    unscored = {"outcome": {"success": None, "failure_type": "unscored"}}
    assert stats.answer_kind_rate([unscored]) is None  # 채점된 에피소드만 센다


def test_ambiguity_ignored_counts_unmatched_as_none() -> None:
    rows = [
        _row("ambiguity_ignored", expected="clarify", reading=0),
        _row("ambiguity_ignored", expected="clarify", reading=None),
        _row("ambiguity_ignored", expected="clarify", reading=0),
        _row(None, expected="clarify"),
    ]
    assert stats.ambiguity_ignored_by_reading(rows) == {"0": 2, "none": 1}


# ------------------------------------------------------------ 채점 우선순위


def _gold(expected: dict[str, Any], **kw: Any) -> Gold:
    return Gold.model_validate({"expected_answer": expected, "expected_changes": [], **kw})


def test_failure_priority_between_new_types() -> None:
    event = {"kind": "event", "event": EV1}
    gold = _gold(event)
    s = score_episode(gold, "", [], WorldDiff(), {"kind": "event_set", "events": [EV1]})
    assert (s.success, s.failure_type, s.checks["structured_answer"]) == (
        False,
        "answer_kind",
        False,
    )
    # 형식 오류는 answer_kind보다 앞선다
    s = score_episode(gold, "", [], WorldDiff(), {"kind": "event_set", "events": [EV1, EV1]})
    assert s.failure_type == "answer_format"
    # answer_kind(구조화 답 불일치)보다 forbidden_tool과 missing_tool_call이 앞선다
    calls = [CallRecord(seq=1, tool="cancel_event", args={}, outcome="ok")]
    s = score_episode(
        _gold(event, forbidden_tools=["cancel_event"]),
        "",
        calls,
        WorldDiff(),
        {"kind": "clarify"},
    )
    assert s.failure_type == "forbidden_tool"
    s = score_episode(
        _gold(event, required_tool_calls=[{"tool": "list_events"}]),
        "",
        [],
        WorldDiff(),
        {"kind": "clarify"},
    )
    assert s.failure_type == "missing_tool_call"
    # unneeded_clarify는 must_not_contain 위반(wrong_answer)보다 앞선다
    s = score_episode(
        _gold(event, must_not_contain=["금지"]), "금지", [], WorldDiff(), {"kind": "clarify"}
    )
    assert s.failure_type == "unneeded_clarify"


def test_score_matched_reading_only_for_ambiguity_ignored() -> None:
    gold = _gold(EXPECTED["v-clarify"])
    s = score_episode(gold, "", [], WorldDiff(), {"kind": "event", "event": EV1})
    assert (s.failure_type, s.matched_reading) == ("ambiguity_ignored", 0)
    s = score_episode(gold, "", [], WorldDiff(), {"kind": "clarify"})
    assert (s.success, s.matched_reading) == (True, None)
