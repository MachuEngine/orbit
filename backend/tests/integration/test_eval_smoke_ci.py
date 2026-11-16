"""CI 스모크 회귀 (ADR-0026 §5, 평가 계획 "프롬프트·모델 변경 회귀: 스모크(CI)").

목적은 프롬프트·도구·러너·채점·데이터 로드의 연결이 깨졌을 때 CI가 실패하게 하는 것이다. 모델 품질을
재는 테스트가 아니다. 실제 LLM은 부르지 않고(비용 0), 정답을 그대로 제출하는
스크립트 LLM(오라클)으로
커밋된 세계 v1의 스모크 과제를 두 평가 대상(`runflow_v0`, `assistant_v0`)에 돌린다.
오답 오라클로 채점이 실패를 실제로 잡는지도 확인한다. 데이터는 모두 합성 값이다.

manifest의 `smoke_task_ids` 일부는 사람 검수에서 제외돼(review.jsonl) 데이터셋에 없다. 그런 과제는
실행 대상이 아니며, 목록과 제외 기록이 어긋나면 이 테스트가 실패한다.
"""

import json
import os
import subprocess  # noqa: S404  CLI를 별도 프로세스로 실행한다
import sys
from pathlib import Path
from typing import Any

import pytest

from eval_tasks_support import COMMITTED_V1, oracle_script, step
from orbit.evalrunner.llm_script import scripted_llm_factory
from orbit.evalrunner.runflow_env import RunflowEnvironment
from orbit.evalrunner.runner import ExperimentSummary, run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import Dataset, load_dataset

# assistant_v0가 지원하지 않는 과제의 기대 판정(task_id -> 실패 유형).
# 여기에 없는 과제는 성공해야 한다.
# - 쓰기·다중 턴 쓰기: 쓰기 도구가 없어 기대한 상태 변화가 일어나지 않는다.
# - 문서 질의: 문서 도구가 없어 스크립트의 문서 검색 호출이 실행되지 못한다
#   (스크립트 소진 = sut_error).
ASSISTANT_V0_EXPECTED_FAILURES = {
    "mt-mail-fact-0032": "wrong_state_change",
    "mt-mail-fact-0034": "wrong_state_change",
    "t-meeting-request-0005-fault": "wrong_state_change",
    "t-doc-fact-0020": "sut_error",
}

_cache: dict[str, Any] = {}


def _smoke_dataset() -> Dataset:
    if "dataset" not in _cache:
        ds = load_dataset(COMMITTED_V1)
        ids = set(ds.manifest["smoke_task_ids"])
        excluded = {
            r["task_id"]
            for r in map(json.loads, (COMMITTED_V1 / "review.jsonl").read_text().splitlines())
            if r["decision"] == "exclude"
        }
        tasks = tuple(t for t in ds.tasks if t.task_id in ids)
        # 데이터셋에 없는 스모크 id는 검수 제외 기록과 정확히 일치해야 한다.
        assert ids - {t.task_id for t in tasks} == ids & excluded
        assert len(ids) == 25 and len(tasks) >= 20
        assert all(t.split == "dev" for t in tasks)
        # 검색용 계정(메일 수천 통)은 빼서 에피소드 준비를 줄인다. 스모크 과제는 e2e 계정만 쓴다.
        accounts = {
            k: v for k, v in ds.world_snapshot["accounts"].items() if not k.startswith("acct-s")
        }
        assert all(t.account_id in accounts for t in tasks)
        _cache["dataset"] = Dataset(
            ds.name, ds.version, ds.manifest, {**ds.world_snapshot, "accounts": accounts}, tasks
        )
    dataset: Dataset = _cache["dataset"]
    return dataset


async def _oracle(sut: str) -> dict[str, list[dict[str, Any]]]:
    key = f"oracle:{sut}"
    if key not in _cache:
        ds = _smoke_dataset()
        _cache[key] = {
            t.task_id: await oracle_script(ds.world_snapshot, t, runflow=sut == "runflow_v0")
            for t in ds.tasks
        }
    oracle: dict[str, list[dict[str, Any]]] = _cache[key]
    return oracle


async def _run(
    sut: str, script: dict[str, list[dict[str, Any]]], app_url: str, only: list[str] | None = None
) -> ExperimentSummary:
    ds = _smoke_dataset()
    if only is not None:
        ds = Dataset(
            ds.name,
            ds.version,
            ds.manifest,
            ds.world_snapshot,
            tuple(t for t in ds.tasks if t.task_id in only),
        )
    return await run_experiment(
        ds,
        split="dev",
        sut_factory=lambda: create_sut(sut),
        llm_factory=scripted_llm_factory(script),
        repeats=1,
        git_sha="test-sha",
        database_env=RunflowEnvironment(app_url) if sut == "runflow_v0" else None,
    )


async def test_runflow_v0_succeeds_on_every_smoke_task(app_url: str) -> None:
    summary = await _run("runflow_v0", await _oracle("runflow_v0"), app_url)
    assert len(summary.results) == len(_smoke_dataset().tasks)
    failures = [
        f"{r.task_id}: {r.failure_type} {r.detail}" for r in summary.results if not r.success
    ]
    assert not failures, failures


async def test_assistant_v0_matches_the_expected_verdict_table(app_url: str) -> None:
    summary = await _run("assistant_v0", await _oracle("assistant_v0"), app_url)
    ids = {t.task_id for t in _smoke_dataset().tasks}
    assert set(ASSISTANT_V0_EXPECTED_FAILURES) <= ids  # 표가 데이터와 어긋나면 실패한다
    actual = {r.task_id: r.failure_type for r in summary.results if not r.success}
    assert actual == ASSISTANT_V0_EXPECTED_FAILURES
    assert len(summary.results) == len(ids)


async def test_smoke_covers_the_task_kinds_the_spec_requires() -> None:
    tasks = _smoke_dataset().tasks
    kinds = {next(t for t in x.tags if t.startswith("type:")) for x in tasks}
    assert {"type:multi_turn", "type:ambiguous", "type:unanswerable", "type:write"} <= kinds
    assert any(t.fault_plan for t in tasks)  # 장애 주입
    assert any(len(t.turns) > 1 for t in tasks)  # 다중 턴


async def test_wrong_oracles_are_caught_by_scoring(app_url: str) -> None:
    ds = _smoke_dataset()
    oracle = await _oracle("runflow_v0")
    # 정답이 message_set인 일반 메일 질의와, 그것과 답이 다른 다른 메일 질의
    mail, other = "t-mail-fact-0012", "t-mail-fact-0030"
    wrong_answer = {mail: oracle[other]}  # 다른 질문의 정답 제출
    wrong_state = {  # 쓰기 요청인데 아무것도 실행하지 않고 글로만 답한다
        "mt-mail-fact-0034": [step(calls=[]), step("조회했습니다."), step("처리했습니다.")]
    }
    unneeded = {  # 한 가지 뜻인 질문에 되묻기를 제출한다
        mail: [step("되묻습니다.") | {"structured": {"kind": "clarify"}}, step("정리했습니다.")]
    }
    empty = {"t-free-slot-0008": [step("")]}  # 빈 답
    assert {mail, other, "mt-mail-fact-0034", "t-free-slot-0008"} <= {t.task_id for t in ds.tasks}
    cases: list[tuple[dict[str, list[dict[str, Any]]], str, str | None]] = [
        (wrong_answer, mail, "wrong_answer"),
        (wrong_state, "mt-mail-fact-0034", "wrong_state_change"),
        (unneeded, mail, "unneeded_clarify"),
        (empty, "t-free-slot-0008", None),  # 실패 유형은 고정하지 않고 실패만 확인한다
    ]
    for script, task_id, expected in cases:
        summary = await _run("runflow_v0", script, app_url, only=[task_id])
        (r,) = summary.results
        assert not r.success, task_id
        if expected is not None:
            assert r.failure_type == expected, (task_id, r.failure_type, r.detail)


@pytest.mark.parametrize("sut", ["runflow_v0", "assistant_v0"])
async def test_oracle_scripts_exist_for_every_smoke_task(sut: str) -> None:
    oracle = await _oracle(sut)
    assert set(oracle) == {t.task_id for t in _smoke_dataset().tasks}
    assert all(oracle.values())


def _cli(*args: str, app_url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ORBIT_EVAL_DATABASE_URL": app_url}  # 평가 DB 대신 테스트 DB(앱 역할)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "orbit.evalrunner", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


async def test_cli_smoke_run_with_oracle_script_succeeds_on_every_task(
    app_url: str, tmp_path: Path
) -> None:
    script = tmp_path / "oracle.json"
    script.write_text(json.dumps(await _oracle("runflow_v0")), encoding="utf-8")
    out = tmp_path / "runs" / "smoke.jsonl"
    done = _cli(
        "run", "--dataset", str(COMMITTED_V1), "--split", "dev", "--subset", "smoke",
        "--sut", "runflow_v0", "--llm-script", str(script), "--repeats", "1",
        "--out", str(out),
        app_url=app_url,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert "검수 제외 과제 4개" in done.stderr  # 건너뛴 수를 알린다
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(_smoke_dataset().tasks)
    assert all(r["outcome"]["success"] is True for r in rows)
    assert {r["config"]["smoke_excluded"] for r in rows} == {4}
    stats = _cli("stats", str(out), app_url=app_url)
    assert stats.returncode == 0, stats.stderr
    assert json.loads(stats.stdout)["success_rate"]["rate"] == 1.0
