from typing import Any

from orbit.connectors.sim.calls import CallRecord
from orbit.connectors.sim.world import EventCancelled, WorldDiff
from orbit.evalrunner.scoring import normalize_text, score_episode
from orbit.evalrunner.task_source import FactRecord, GoldRecord, RequiredCallRecord

EMPTY = WorldDiff()
CHANGED = WorldDiff(events_cancelled=(EventCancelled("acct", "cal", "ev"),))


def gold(**kw: Any) -> GoldRecord:
    base: dict[str, Any] = {
        "answer_facts": (FactRecord("f1", ("2026-11-12", "11월 12일"), "날짜"),),
        "must_not_contain": (),
        "expected_changes": None,
        "required_tool_calls": (),
        "forbidden_tools": (),
    }
    return GoldRecord(**{**base, **kw})


def rec(tool: str, seq: int = 1, **args: Any) -> CallRecord:
    return CallRecord(seq=seq, tool=tool, args=args, outcome="ok")


def test_normalize_ignores_whitespace_and_case() -> None:
    assert normalize_text(" A b\tC\n") == "abc"


def test_fact_matches_any_of_with_whitespace_and_case_insensitive() -> None:
    g = gold(answer_facts=(FactRecord("f", ("Weekly Sync",), ""),))
    assert score_episode(g, "일정은 weekly   sync 입니다", [], EMPTY).success
    assert score_episode(gold(), "11월  12일에 있습니다", [], EMPTY).success
    assert not score_episode(gold(), "11월 13일입니다", [], EMPTY).success


def test_all_facts_required() -> None:
    g = gold(answer_facts=(FactRecord("a", ("가",), ""), FactRecord("b", ("나",), "")))
    assert score_episode(g, "가 나", [], EMPTY).success
    s = score_episode(g, "가", [], EMPTY)
    assert (s.success, s.failure_type) == (False, "wrong_answer")


def test_must_not_contain() -> None:
    g = gold(must_not_contain=("취소됨",))
    s = score_episode(g, "2026-11-12 일정이 취 소 됨", [], EMPTY)
    assert (s.success, s.failure_type) == (False, "wrong_answer")


def test_expected_changes_none_is_not_checked() -> None:
    s = score_episode(gold(), "2026-11-12", [], CHANGED)
    assert s.success and s.state_diff_match is None and s.unscored == ()


def test_expected_changes_empty_requires_empty_diff() -> None:
    g = gold(expected_changes=())
    assert score_episode(g, "2026-11-12", [], EMPTY).state_diff_match is True
    s = score_episode(g, "2026-11-12", [], CHANGED)
    assert (s.success, s.failure_type, s.state_diff_match) == (
        False,
        "unexpected_state_change",
        False,
    )


def test_expected_changes_nonempty_is_unscored() -> None:
    g = gold(expected_changes=({"type": "event_created"},))
    s = score_episode(g, "2026-11-12", [], EMPTY)
    assert s.success and s.state_diff_match is None and s.unscored == ("expected_changes",)


def test_required_tool_call_name_and_args_subset() -> None:
    g = gold(required_tool_calls=(RequiredCallRecord("list_events", {"calendar_id": "c1"}),))
    ok = [rec("list_events", calendar_id="c1", time_min="x")]
    assert score_episode(g, "2026-11-12", ok, EMPTY).success
    s = score_episode(g, "2026-11-12", [rec("list_events", calendar_id="c2")], EMPTY)
    assert s.failure_type == "missing_tool_call"
    s = score_episode(g, "2026-11-12", [rec("get_event", calendar_id="c1")], EMPTY)
    assert s.failure_type == "missing_tool_call"


def test_args_subset_compares_aware_datetimes_as_instants() -> None:
    g = gold(
        required_tool_calls=(
            RequiredCallRecord("list_events", {"time_min": "2026-11-12T00:00:00+09:00"}),
        )
    )
    recorded = [rec("list_events", time_min="2026-11-11 15:00:00+00:00")]  # str(datetime) 형태
    assert score_episode(g, "2026-11-12", recorded, EMPTY).success


def test_forbidden_tool_counts_failed_attempts_too() -> None:
    g = gold(forbidden_tools=("create_event",))
    call = CallRecord(seq=1, tool="create_event", args={}, outcome="error", error_type="X")
    s = score_episode(g, "2026-11-12", [call], EMPTY)
    assert (s.success, s.failure_type) == (False, "forbidden_tool")


def test_failure_priority() -> None:
    g = gold(
        forbidden_tools=("cancel_event",),
        expected_changes=(),
        required_tool_calls=(RequiredCallRecord("list_events", {}),),
    )
    calls = [rec("cancel_event")]
    assert score_episode(g, "틀림", calls, CHANGED).failure_type == "forbidden_tool"
    g2 = gold(expected_changes=(), required_tool_calls=(RequiredCallRecord("list_events", {}),))
    assert score_episode(g2, "틀림", [], CHANGED).failure_type == "unexpected_state_change"
    assert score_episode(g2, "틀림", [], EMPTY).failure_type == "missing_tool_call"
    assert score_episode(gold(), "틀림", [], EMPTY).failure_type == "wrong_answer"
