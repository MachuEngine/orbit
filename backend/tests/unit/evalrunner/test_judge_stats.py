"""미니 합성 기록으로 namespace·집계 대응·분모·정상 재개 이력을 검증한다."""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner import judge, metrics_v1, stats
from orbit.evalrunner import judge_stats as js

MODEL = "google/synthetic-judge"


def records(sid: str) -> list[dict[str, Any]]:
    config = {
        "dataset": "synthetic@v1",
        "split": "test",
        "repeats": 1,
        "n_tasks": 4,
        "sut": {"name": "runflow_v0", "version": "0"},
        "model_id": f"synthetic/{sid}",
    }
    return [
        {
            "task_id": f"task-{i}",
            "repeat": 1,
            "cluster_id": f"cluster-{max(0, i - 1)}",
            "config": copy.deepcopy(config),
            "config_fingerprint": js.config_digest(config),
            "outcome": {"success": True, "failure_type": None},
            "model_mismatch": False,
            "trace": {
                "turns": [{"user": "합성 요청입니다."}],
                "final_text": "확인했습니다.",
                "tool_calls": [
                    {
                        "name": "read_mail",
                        "result": "합성 데이터",
                        "is_error": False,
                        "error_kind": None,
                    }
                ],
            },
            "task_meta": {"type": "mail"},
        }
        for i in range(4)
    ]


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), "utf-8")


def manifest_at(
    tmp_path: Path, a: list[dict[str, Any]] | None = None, b: list[dict[str, Any]] | None = None
) -> Path:
    settings = []
    for sid, rows in (("luna-index", a), ("other", b)):
        rows = rows if rows is not None else records(sid)
        write_rows(tmp_path / f"{sid}.jsonl", rows)
        settings.append(
            {
                "id": sid,
                "results": f"{sid}.jsonl",
                "config_fingerprint": rows[0]["config_fingerprint"],
                "results_sha256": hashlib.sha256(
                    (tmp_path / f"{sid}.jsonl").read_bytes()
                ).hexdigest(),
                "expected_config": {"sut": {"name": "runflow_v0"}},
            }
        )
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "baseline": "luna-index",
                "expected_tasks": 4,
                "expected_clusters": 3,
                "expected_settings": 2,
                "settings": settings,
            }
        ),
        "utf-8",
    )
    return path


def judgment(
    row: dict[str, Any], grounded: bool = True, *, cost: float = 0.02, status: str = "ok"
) -> dict[str, Any]:
    ep = js.as_episode(row)
    return {
        "item_id": ep.episode_id,
        "task_id": ep.task_id,
        "repeat": ep.repeat,
        "sut": ep.sut,
        "cluster_id": ep.cluster_id,
        "reviewer_family": "google",
        "model_id": MODEL,
        "model_config": {"model_id": MODEL, "route": "openrouter", "temperature": 0.0},
        "prompt_version": judge.PROMPT_VERSION,
        "input_sha": judge.input_sha(ep),
        "status": status,
        "grounded": grounded if status == "ok" else None,
        "claims": {"supported": int(grounded), "unsupported": int(not grounded), "not_checkable": 0}
        if status == "ok"
        else None,
        "parse_failed": False,
        "cost": cost,
        "attempts": 1,
    }


def summarize(manifest: js.Manifest, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return js.summarize(manifest, rows, family="google", model_id=MODEL, resamples=100, seed=0)


def test_prepare_namespaces_same_episode_and_includes_only_recorded_error_data(
    tmp_path: Path,
) -> None:
    a = records("luna-index")
    call = a[0]["trace"]["tool_calls"][0]
    call.update(
        {
            "result": '{"error":"permission_revoked"}',
            "is_error": True,
            "error_kind": "permission_revoked",
        }
    )
    a[1]["trace"]["tool_calls"] = []  # 정상적인 빈 기록은 eligible이다.
    manifest = js.load_manifest(manifest_at(tmp_path, a))
    inputs, counts = js.prepare(manifest)
    assert len(inputs) == len({judge.episode_id_of(row) for row in inputs}) == 8
    assert {row["sut"] for row in inputs} == {"luna-index::runflow_v0", "other::runflow_v0"}
    assert inputs[0]["source_sut"] == "runflow_v0"
    assert inputs[0]["source_config_fingerprint"] == a[0]["config_fingerprint"]
    assert json.loads(inputs[0]["tool_results"][0]["result"]) == {
        "recorded_result": '{"error":"permission_revoked"}',
        "recorded_error_kind": "permission_revoked",
    }
    assert inputs[1]["tool_results"] == [] and counts["luna-index"]["eligible"] == 4
    assert next(metrics_v1.judge_inputs(a))["tool_results"] == []  # 기존 경로는 그대로다.
    write_rows(tmp_path / "prepared.jsonl", inputs)
    loaded, skipped = judge.load_episodes(tmp_path / "prepared.jsonl")
    assert skipped == 0 and judge.input_sha(loaded[0]) == judge.input_sha(js.as_episode(inputs[0]))


def test_fixed_denominators_keep_exclusions_missing_and_unscored_rows(tmp_path: Path) -> None:
    a = records("luna-index")
    a[2]["trace"]["final_text"] = " "
    a[2]["outcome"]["success"] = False
    a[3]["trace"] = None
    a[3]["outcome"]["success"] = None
    before = copy.deepcopy(a)
    manifest = js.load_manifest(manifest_at(tmp_path, a))
    inputs, _ = js.prepare(manifest)
    journal = [
        judgment(row, grounded=row["task_id"] != "task-1")
        for row in inputs
        if not (row["setting_id"] == "luna-index" and row["task_id"] == "task-1")
    ]
    report = summarize(manifest, journal)
    baseline, other = report["settings"]["luna-index"], report["settings"]["other"]
    assert baseline["total"] == 4 and baseline["eligible"] == 2
    assert baseline["excluded"] == {"no_answer": 1, "no_judge_recording": 1}
    assert baseline["missing_verdicts"] == 1 and baseline["missing_reasons"] == {"absent_row": 1}
    assert baseline["primary_success"]["rate"] == pytest.approx(2 / 3)
    assert baseline["primary_success"]["n_unscored_episodes"] == 1
    assert baseline["strict_success_fixed_all"]["rate"] == 0.25
    assert baseline["strict_success_fixed_all"]["n_episodes"] == 4
    assert baseline["grounded_fixed_eligible"]["rate"] == 0.5
    assert baseline["grounded_conditional_verdict"]["rate"] == 1.0
    assert other["paired_a_minus_baseline"]["strict_success_fixed_all"]["difference"] == 0.5
    assert other["paired_a_minus_baseline"]["grounded_conditional_verdict"]["n_tasks"] == 1
    assert baseline["claim_counts"] == {"supported": 1, "unsupported": 0, "not_checkable": 0}
    assert baseline["code_fact_grounded_rate"] == metrics_v1.fact_support(a)["support_rate"]
    assert a == before and manifest.settings[0].records[3]["outcome"]["success"] is None
    assert report == summarize(manifest, journal)  # 시드와 묶음 재표본이 결정적이다.


def test_rates_and_paired_ci_reuse_existing_shared_cluster_logic(tmp_path: Path) -> None:
    manifest = js.load_manifest(manifest_at(tmp_path))
    inputs, _ = js.prepare(manifest)
    journal = [
        judgment(row, row["setting_id"] == "other" or row["task_id"] == "task-0") for row in inputs
    ]
    report = summarize(manifest, journal)
    a = [{**row, "outcome": {"success": True}} for row in manifest.settings[1].records]
    b = [
        {**row, "outcome": {"success": row["task_id"] == "task-0"}}
        for row in manifest.settings[0].records
    ]
    expected = stats.paired_difference(a, b, resamples=100, seed=0, include_incomplete=True)
    actual = report["settings"]["other"]["paired_a_minus_baseline"]["grounded_fixed_eligible"]
    assert (actual["difference"], actual["ci_low"], actual["ci_high"], actual["n_clusters"]) == (
        expected.difference,
        expected.ci_low,
        expected.ci_high,
        expected.n_clusters,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("item_id", "other-sut/task-0/1"),
        ("input_sha", "different-input"),
        ("prompt_version", "judge-v2"),
        ("cluster_id", "other-cluster"),
        ("task_id", "other-task"),
        ("repeat", True),
        ("sut", "runflow_v0"),
        ("model_id", "google/other-model"),
        ("reviewer_family", "anthropic"),
        ("model_config", {"model_id": "google/other-model"}),
    ],
)
def test_wrong_judge_identity_input_version_model_or_cluster_is_rejected(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    manifest = js.load_manifest(manifest_at(tmp_path))
    inputs, _ = js.prepare(manifest)
    row = judgment(inputs[0])
    row[field] = value
    with pytest.raises(js.JudgeStatsError):
        summarize(manifest, [row])


def test_normal_abort_then_ok_resume_preserves_all_attempt_costs(tmp_path: Path) -> None:
    manifest = js.load_manifest(manifest_at(tmp_path))
    inputs, _ = js.prepare(manifest)
    aborted = judgment(inputs[0], cost=0.01, status="aborted")
    completed = judgment(inputs[0], cost=0.02)
    setting = summarize(manifest, [aborted, completed])["settings"]["luna-index"]
    assert setting["valid_verdicts"] == 1
    assert setting["judge_history"] == {
        "history_rows": 2,
        "aborted_rows": 1,
        "aborted_attempts": 1,
        "cost_usd_all_history": pytest.approx(0.03),
    }


@pytest.mark.parametrize("later_status", ["ok", "aborted"])
def test_completed_verdict_cannot_be_duplicated_or_followed_by_abort(
    tmp_path: Path, later_status: str
) -> None:
    manifest = js.load_manifest(manifest_at(tmp_path))
    inputs, _ = js.prepare(manifest)
    with pytest.raises(js.JudgeStatsError, match="완료 Judge 판정"):
        summarize(manifest, [judgment(inputs[0]), judgment(inputs[0], status=later_status)])


def test_parse_failure_is_missing_not_an_eligible_denominator_reduction(tmp_path: Path) -> None:
    manifest = js.load_manifest(manifest_at(tmp_path))
    inputs, _ = js.prepare(manifest)
    failed = {**judgment(inputs[0]), "grounded": None, "claims": None, "parse_failed": True}
    setting = summarize(manifest, [failed])["settings"]["luna-index"]
    assert setting["grounded_fixed_eligible"]["rate"] == 0
    assert setting["grounded_fixed_eligible"]["n_tasks"] == 4
    assert setting["grounded_conditional_verdict"]["available"] is False
    assert setting["missing_reasons"] == {"absent_row": 3, "parse_failed": 1}


@pytest.mark.parametrize(
    "change", ["fingerprint", "config_content", "duplicate", "count", "cluster_map"]
)
def test_raw_config_episode_and_cluster_contracts_are_strict(tmp_path: Path, change: str) -> None:
    a, b = records("luna-index"), records("other")
    if change == "fingerprint":
        a[0]["config_fingerprint"] = "wrong"
    elif change == "config_content":
        a[0]["config"]["model_id"] = "synthetic/wrong"
    elif change == "duplicate":
        a[1] = copy.deepcopy(a[0])
    elif change == "count":
        a.pop()
    else:
        b[0]["cluster_id"] = "cluster-2"
    with pytest.raises(js.JudgeStatsError):
        js.load_manifest(manifest_at(tmp_path, a, b))


def test_prepare_allows_partial_setting_batch_but_summary_requires_declared_setting_count(
    tmp_path: Path,
) -> None:
    path = manifest_at(tmp_path)
    raw = json.loads(path.read_text())
    raw["settings"] = raw["settings"][1:]
    path.write_text(json.dumps(raw))
    manifest = js.load_manifest(path)
    assert len(js.prepare(manifest)[0]) == 4
    with pytest.raises(js.JudgeStatsError, match="baseline"):
        summarize(manifest, [])


def test_cli_prepare_is_immutable_and_summary_uses_ten_thousand_seed_zero(
    tmp_path: Path, capsys: Any
) -> None:
    path = manifest_at(tmp_path)
    prepared = tmp_path / "input.jsonl"
    command = ["prepare", "--manifest", str(path), "--out", str(prepared)]
    assert js.main(command) == 0
    before = prepared.read_bytes()
    assert js.main(command) == 2 and prepared.read_bytes() == before
    inputs = [json.loads(line) for line in prepared.read_text().splitlines()]
    journal = tmp_path / "judge.jsonl"
    write_rows(journal, [judgment(row) for row in inputs])
    out = tmp_path / "summary.json"
    assert (
        js.main(
            [
                "summarize",
                "--manifest",
                str(path),
                "--judge",
                str(journal),
                "--family",
                "google",
                "--model-id",
                MODEL,
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads(out.read_text())
    assert report["bootstrap"] == {
        "resamples": 10_000,
        "seed": 0,
        "unit": "shared_cluster",
        "ci": 0.95,
    }
    capsys.readouterr()


@pytest.mark.parametrize("hash_value", [None, "wrong", "0" * 64])
def test_original_result_bytes_hash_is_required_and_checked(
    tmp_path: Path, hash_value: Any
) -> None:
    path = manifest_at(tmp_path)
    raw = json.loads(path.read_text())
    if hash_value is None:
        del raw["settings"][0]["results_sha256"]
    else:
        raw["settings"][0]["results_sha256"] = hash_value
    path.write_text(json.dumps(raw))
    with pytest.raises(js.JudgeStatsError, match="bytes SHA256"):
        js.load_manifest(path)


def test_changing_only_success_keeps_judge_input_hash_but_invalidates_original_file(
    tmp_path: Path,
) -> None:
    path = manifest_at(tmp_path)
    before = js.load_manifest(path)
    inputs_before, _ = js.prepare(before)
    changed = records("luna-index")
    changed[0]["outcome"]["success"] = False
    prepared_after, _ = js.prepare_record("luna-index", changed[0])
    assert prepared_after is not None
    assert judge.input_sha(js.as_episode(inputs_before[0])) == judge.input_sha(
        js.as_episode(prepared_after)
    )
    write_rows(tmp_path / "luna-index.jsonl", changed)
    with pytest.raises(js.JudgeStatsError, match="결과 원본 bytes SHA256가 다르다"):
        js.load_manifest(path)
