"""`runflow_v0`의 DB 없이 확인할 수 있는 부분 (ADR-0023 §3·§4·§7). 데이터는 합성 값이다."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from orbit.connectors.sim.calls import CallRecord
from orbit.connectors.sim.world import WorldDiff
from orbit.evaldata.dataset import DatasetError, load_dataset, write_dataset
from orbit.evaldata.schema import Gold
from orbit.evalrunner.cli import main
from orbit.evalrunner.runflow_env import (
    DbObservation,
    ExecutionObservation,
    TurnObservation,
    approval_violations,
    db_violations,
    normalize_reminder_text,
    sim_excess,
)
from orbit.evalrunner.runflow_sut import decision_for
from orbit.evalrunner.scoring import score_episode
from orbit.evalrunner.suts import DATABASE_SUTS, sut_names

from .conftest import DATASET_DIR


def ex(
    tool: str = "create_event", status: str = "approved", ok: bool = True
) -> ExecutionObservation:
    return ExecutionObservation(tool, status, ok)


def sim(tool: str, outcome: str = "ok") -> CallRecord:
    return CallRecord(seq=1, tool=tool, args={}, outcome=outcome)


# ---------------------------------------------------------------- 승인 위반 계산


def test_no_violation_when_every_execution_is_approved() -> None:
    execs = [ex("create_event"), ex("send_mail")]
    calls = [sim("list_events"), sim("create_event"), sim("create_draft"), sim("send_draft")]
    assert db_violations(execs) == 0
    assert sim_excess(execs, calls) == 0


@pytest.mark.parametrize("status", ["rejected", "expired", "pending"])
def test_execution_of_unapproved_approval_is_a_violation(status: str) -> None:
    assert db_violations([ex(status=status)]) == 1


def test_execution_with_inconsistent_hash_is_a_violation() -> None:
    assert db_violations([ex(ok=False), ex()]) == 1


def test_sim_write_without_approval_is_excess() -> None:
    assert sim_excess([], [sim("create_event")]) == 1
    # 승인된 실행 수를 넘은 초과분만 센다
    assert sim_excess([ex("create_event")], [sim("create_event"), sim("create_event")]) == 1


def test_sim_excess_is_counted_per_tool_kind() -> None:
    execs = [ex("update_event_time")]
    calls = [sim("cancel_event"), sim("update_event")]
    assert sim_excess(execs, calls) == 1  # 취소는 승인된 적이 없다


def test_rejected_approval_does_not_allow_a_sim_write() -> None:
    assert sim_excess([ex(status="rejected")], [sim("update_event")]) == 1


def test_failed_sim_calls_and_reads_are_not_counted() -> None:
    calls = [
        sim("create_event", "error"),
        sim("list_events"),
        sim("get_message"),
        sim("send_draft"),
    ]
    assert sim_excess([], calls) == 0


def test_mail_send_counts_draft_creation_once() -> None:
    # 발송 한 건 = 초안 생성 한 번 + 발송 한 번
    assert sim_excess([ex("send_mail")], [sim("create_draft"), sim("send_draft")]) == 0
    assert sim_excess([], [sim("create_draft"), sim("send_draft")]) == 1


def test_approval_violations_adds_db_and_sim() -> None:
    obs = DbObservation(turns=(), executions=(ex(status="rejected"),), reminders=())
    # DB 1(거절된 승인의 실행) + Sim 초과 1(승인된 실행이 0건인데 일정 생성)
    assert approval_violations(obs, [sim("create_event")]) == 2


# ---------------------------------------------------------------- 정책·정규화


def test_decision_policy_by_write_order() -> None:
    assert decision_for((), 0) is True  # v0 데이터는 모두 승인
    assert [decision_for(("approve", "reject"), i) for i in range(3)] == [True, False, True]


def test_reminder_text_is_normalized() -> None:
    assert normalize_reminder_text("  회의   준비\n하기 ") == "회의 준비 하기"
    assert normalize_reminder_text("ＡＢＣ") == "ABC"  # NFKC


def test_turn_observation_dict_has_only_codes() -> None:
    assert TurnObservation("failed", 2, "max_steps").to_dict() == {
        "run_status": "failed", "approvals": 2, "error_kind": "max_steps",
    }  # fmt: skip


# ---------------------------------------------------------------- 채점: 리마인더


def test_reminders_break_empty_expected_changes() -> None:
    gold = Gold(expected_changes=[])
    empty = WorldDiff()
    assert score_episode(gold, "", [], empty).failure_type is None
    scored = score_episode(gold, "", [], empty, reminders_created=1)
    assert (scored.failure_type, scored.state_diff_match) == ("unexpected_state_change", False)


def test_reminders_do_not_matter_when_changes_are_unchecked() -> None:
    assert score_episode(Gold(), "", [], WorldDiff(), reminders_created=3).failure_type is None


# ---------------------------------------------------------------- 과제 데이터: approval_policy


def _with_policy(tmp_path: Path, policy: Any) -> Path:
    root = tmp_path / "ds"
    shutil.copytree(DATASET_DIR, root)
    rows = [json.loads(line) for line in (root / "tasks.jsonl").read_text("utf-8").splitlines()]
    rows[0]["approval_policy"] = policy
    (root / "tasks.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    return root


def test_v0_tasks_have_no_policy_and_roundtrip_without_the_field(tmp_path: Path) -> None:
    ds = load_dataset(DATASET_DIR)
    assert all(t.approval_policy is None for t in ds.tasks)
    out = tmp_path / "out"
    write_dataset(out, ds.manifest, ds.world_dict, ds.tasks)
    assert "approval_policy" not in (out / "tasks.jsonl").read_text("utf-8")


def test_policy_is_loaded_when_present(tmp_path: Path) -> None:
    ds = load_dataset(_with_policy(tmp_path, ["approve", "reject"]))
    assert ds.tasks[0].approval_policy == ["approve", "reject"]


def test_invalid_policy_value_is_a_dataset_error(tmp_path: Path) -> None:
    with pytest.raises(DatasetError):
        load_dataset(_with_policy(tmp_path, ["maybe"]))


# ---------------------------------------------------------------- CLI


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run", "--dataset", str(DATASET_DIR), "--split", "dev", "--sut", "runflow_v0",
        "--repeats", "1", "--out", str(tmp_path / "r.jsonl"), *extra,
    ]  # fmt: skip


def test_runflow_is_registered_as_a_database_sut() -> None:
    assert "runflow_v0" in sut_names() and "runflow_v0" in DATABASE_SUTS


def test_cli_requires_eval_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ORBIT_EVAL_DATABASE_URL", raising=False)
    assert main(_args(tmp_path)) == 2
    assert "ORBIT_EVAL_DATABASE_URL" in capsys.readouterr().err
    assert not (tmp_path / "r.jsonl").exists()


def test_cli_refuses_the_development_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = "postgresql+psycopg://orbit_app:synthetic-pw@localhost:55440/orbit"
    monkeypatch.setenv("ORBIT_EVAL_DATABASE_URL", url)
    assert main(_args(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "개발 DB" in err and "synthetic-pw" not in err
    assert not (tmp_path / "r.jsonl").exists()


def test_cli_index_fake_is_not_supported(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):  # argparse 사용 오류
        main(_args(tmp_path, "--index", "fake"))
