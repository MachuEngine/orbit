"""로더 검증 실패 사례, 검수 export/apply 왕복, 세계 모델 검증."""

import json
import shutil
import tomllib
from pathlib import Path
from typing import Any

import pytest

from orbit.evaldata.dataset import DatasetError, load_dataset, read_review
from orbit.evaldata.review import apply_decisions, export_markdown, main
from orbit.evaldata.worldmodel import WorldModelError, load_world_model, parse_world_model

from .conftest import WORLD_MODEL


@pytest.fixture
def ds_copy(dataset_dir: Path, tmp_path: Path) -> Path:
    out = tmp_path / "copy"
    shutil.copytree(dataset_dir, out)
    return out


def _rewrite_tasks(path: Path, mutate: Any) -> None:
    rows = [json.loads(line) for line in (path / "tasks.jsonl").read_text("utf-8").splitlines()]
    mutate(rows)
    (path / "tasks.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def test_unknown_account_rejected(ds_copy: Path) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        rows[0]["account_id"] = "acct-nobody"

    _rewrite_tasks(ds_copy, mutate)
    with pytest.raises(DatasetError, match="acct-nobody"):
        load_dataset(ds_copy)


def test_naive_as_of_rejected(ds_copy: Path) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        rows[0]["as_of_time"] = "2026-11-09T09:00:00"

    _rewrite_tasks(ds_copy, mutate)
    with pytest.raises(DatasetError, match="1번째 줄"):
        load_dataset(ds_copy)


def test_error_message_does_not_echo_input(ds_copy: Path, tmp_path: Path) -> None:
    secret = "SECRET-QUESTION-TEXT"

    def mutate(rows: list[dict[str, Any]]) -> None:
        rows[0]["turns"] = [{"role": secret, "content": secret}]

    _rewrite_tasks(ds_copy, mutate)
    with pytest.raises(DatasetError) as info:
        load_dataset(ds_copy)
    assert "turns" in str(info.value)
    assert secret not in str(info.value)
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"task_id": "day-001", "decision": secret}) + "\n", "utf-8")
    with pytest.raises(DatasetError) as info2:
        apply_decisions(ds_copy, bad)
    assert secret not in str(info2.value)


def test_scenario_in_both_splits_rejected(ds_copy: Path) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        first = rows[0]
        other = next(r for r in rows if r["scenario_id"] != first["scenario_id"])
        other["scenario_id"] = first["scenario_id"]
        other["split"] = "test" if first["split"] == "dev" else "dev"

    _rewrite_tasks(ds_copy, mutate)
    with pytest.raises(DatasetError, match="dev와 test에 모두"):
        load_dataset(ds_copy)


def test_cluster_violations_rejected(ds_copy: Path) -> None:
    def split_cluster(rows: list[dict[str, Any]]) -> None:
        first = rows[0]
        other = next(r for r in rows if r["scenario_id"] != first["scenario_id"])
        other["cluster_id"] = first["cluster_id"]
        other["split"] = "test" if first["split"] == "dev" else "dev"

    _rewrite_tasks(ds_copy, split_cluster)
    with pytest.raises(DatasetError, match="묶음 .* dev와 test에 모두"):
        load_dataset(ds_copy)


def test_evidence_sharing_tasks_must_share_cluster(ds_copy: Path) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        rows[0]["cluster_id"] = "c-forged"

    _rewrite_tasks(ds_copy, mutate)
    with pytest.raises(DatasetError, match="서로 다른 묶음"):
        load_dataset(ds_copy)


def test_cluster_id_is_required(ds_copy: Path) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        del rows[0]["cluster_id"]

    _rewrite_tasks(ds_copy, mutate)
    with pytest.raises(DatasetError, match="cluster_id"):
        load_dataset(ds_copy)


def test_duplicate_task_id_and_count_mismatch_rejected(ds_copy: Path) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        rows[1]["task_id"] = rows[0]["task_id"]

    _rewrite_tasks(ds_copy, mutate)
    with pytest.raises(DatasetError, match="task_id 중복"):
        load_dataset(ds_copy)
    manifest = json.loads((ds_copy / "manifest.json").read_text("utf-8"))
    manifest["counts"]["events"] += 1
    (ds_copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetError, match="counts"):
        load_dataset(ds_copy)


def test_missing_file_rejected(ds_copy: Path) -> None:
    (ds_copy / "world.json").unlink()
    with pytest.raises(DatasetError, match="world.json"):
        load_dataset(ds_copy)


def test_review_roundtrip(ds_copy: Path, tmp_path: Path) -> None:
    ds = load_dataset(ds_copy)
    ids = [t.task_id for t in ds.tasks]
    md = export_markdown(ds_copy)
    assert all(f"## {i} " in md for i in ids)
    assert "근거 일정" in md and "근거 메일" in md and "판단:" in md
    assert ds.tasks[0].turns[0].content in md

    decisions = tmp_path / "decisions.jsonl"
    rows = [
        {"task_id": ids[0], "decision": "approve", "note": ""},
        {"task_id": ids[1], "decision": "exclude", "note": "질문이 모호함"},
        {"task_id": ids[2], "decision": "fix", "note": "표현 보강"},
    ]
    decisions.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    apply_decisions(ds_copy, decisions)

    review = read_review(ds_copy)
    assert {k: v.decision for k, v in review.items()} == {
        ids[0]: "approve",
        ids[1]: "exclude",
        ids[2]: "fix",
    }
    loaded = load_dataset(ds_copy)
    assert ids[1] not in [t.task_id for t in loaded.tasks]
    assert len(loaded.tasks) == len(ids) - 1
    assert len(load_dataset(ds_copy, include_excluded=True).tasks) == len(ids)
    notes = loaded.manifest.notes
    assert "검수 현황: 검수 3/60과제 (approve 1, fix 1, exclude 1)" in notes
    assert loaded.manifest.counts.tasks == len(ids)

    # 같은 과제의 새 결정이 이전 결정을 덮고 notes 줄은 하나만 남는다
    decisions.write_text(
        json.dumps({"task_id": ids[1], "decision": "approve", "note": "수정함"}) + "\n", "utf-8"
    )
    apply_decisions(ds_copy, decisions)
    again = load_dataset(ds_copy)
    assert len(again.tasks) == len(ids)
    assert again.manifest.notes.count("검수 현황:") == 1
    assert "approve 2, fix 1, exclude 0" in again.manifest.notes


def test_apply_rejects_unknown_task_and_bad_decision(ds_copy: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"task_id": "nope-001", "decision": "approve"}\n', "utf-8")
    with pytest.raises(DatasetError, match="nope-001"):
        apply_decisions(ds_copy, bad)
    bad.write_text('{"task_id": "day-001", "decision": "maybe"}\n', "utf-8")
    with pytest.raises(DatasetError, match="1번째 줄"):
        apply_decisions(ds_copy, bad)
    assert not (ds_copy / "review.jsonl").exists()


def test_review_cli(ds_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "review.md"
    assert main(["export", str(ds_copy), "--out", str(out)]) == 0
    assert "# 검수표" in out.read_text("utf-8")
    decisions = tmp_path / "d.jsonl"
    decisions.write_text('{"task_id": "day-001", "decision": "approve"}\n', "utf-8")
    assert main(["apply", str(ds_copy), str(decisions)]) == 0
    assert read_review(ds_copy)["day-001"].decision == "approve"


def test_world_model_validation() -> None:
    wm = load_world_model(WORLD_MODEL)
    assert len(wm.users) == 2
    assert 15 <= len(wm.people) <= 25
    assert 3 <= len(wm.projects) <= 4
    assert wm.recurring

    raw = tomllib.loads(WORLD_MODEL.read_text("utf-8"))
    raw["projects"][0]["members"] = ["nobody"]
    with pytest.raises(WorldModelError):
        parse_world_model(raw)

    raw = tomllib.loads(WORLD_MODEL.read_text("utf-8"))
    raw["people"] = [p for p in raw["people"] if p["key"] != "kim_minsu_nuri"]
    raw["projects"][1]["members"] = [
        m for m in raw["projects"][1]["members"] if m != "kim_minsu_nuri"
    ]
    with pytest.raises(WorldModelError, match="동명이인"):
        parse_world_model(raw)
