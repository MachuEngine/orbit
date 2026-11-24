"""rag-v1 봉인: 블록 필드, 재봉인 거부, 파일 누락, 검증 일치·불일치."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orbit.evaldata import dataset as ds
from orbit.evaldata.rag_v1.cli import main
from orbit.evaldata.schema import Counts, Manifest, SplitRatio

NOTE = "표본 검수 완료"


def _write_tasks(root: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    (root / ds.TASKS).write_text(text, encoding="utf-8")


@pytest.fixture
def d(tmp_path: Path) -> Path:
    (tmp_path / ds.WORLD).write_text('{"accounts": {}}', encoding="utf-8")
    (tmp_path / "graph_gold.json").write_text("{}", encoding="utf-8")
    (tmp_path / "skeleton.json").write_text("{}", encoding="utf-8")
    (tmp_path / "review-sample.md").write_text(
        "# 표본\n\n## 검수 방법\n\n## rag-a-01 (test, x)\n- 판정: \n\n## rag-b-02 (dev, y)\n",
        encoding="utf-8",
    )
    _write_tasks(
        tmp_path,
        [
            {"task_id": "t3", "split": "test", "cluster_id": "c2", "q": "하나"},
            {"task_id": "t1", "split": "test", "cluster_id": "c1", "q": "둘"},
            {"task_id": "t2", "split": "test", "cluster_id": "c1", "q": "셋"},
            {"task_id": "t4", "split": "dev", "cluster_id": "c3", "q": "넷"},
        ],
    )
    m = Manifest(
        name="n",
        version="rag-v1",
        generator_git_sha="x",
        seed=1,
        created_with="templates",
        split_ratio=SplitRatio(dev=0.25, test=0.75),
        counts=Counts(users=1, messages=0, events=0, tasks=4),
    )
    ds.write_manifest(tmp_path, m)
    return tmp_path


def _run(*argv: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    return main(list(argv), echo=lines.append), lines


def _seal(d: Path) -> int:
    return _run("seal", "--out", str(d), "--review-note", NOTE)[0]


def _manifest(d: Path) -> dict[str, Any]:
    m: dict[str, Any] = json.loads((d / ds.MANIFEST).read_text(encoding="utf-8"))
    return m


def test_seal_block(d: Path) -> None:
    assert _seal(d) == 0
    m = _manifest(d)
    seal = m["seal"]
    assert seal["schema"] == "rag-seal-v1"
    assert (seal["test_items"], seal["test_clusters"]) == (3, 2)
    assert len(seal["test_sha256"]) == 64 and len(seal["world_sha256"]) == 64
    assert {"tasks_sha256", "graph_gold_sha256", "skeleton_sha256", "sealed_at"} <= seal.keys()
    assert seal["review"] == {
        "human_sampled": 2,
        "sample_seed": 20261121,
        "sample_script": "scripts/rag_v1_review_sample.py",
        "note": NOTE,
    }
    assert m["name"] == "n" and m["counts"]["tasks"] == 4


def test_reseal_refused(d: Path) -> None:
    assert _seal(d) == 0
    before = (d / ds.MANIFEST).read_bytes()
    assert _seal(d) == 2
    assert (d / ds.MANIFEST).read_bytes() == before


@pytest.mark.parametrize(
    "name", [ds.WORLD, ds.TASKS, "graph_gold.json", "skeleton.json", "review-sample.md"]
)
def test_missing_file_refused(d: Path, name: str) -> None:
    (d / name).unlink()
    assert _seal(d) == 2
    assert _manifest(d).get("seal") is None


def test_verify_before_seal(d: Path) -> None:
    code, lines = _run("verify-seal", "--out", str(d))
    assert (code, lines) == (0, ["봉인 전"])


def test_verify_match(d: Path) -> None:
    _seal(d)
    assert _run("verify-seal", "--out", str(d)) == (0, ["봉인 일치"])


def test_test_task_change_mismatch(d: Path) -> None:
    _seal(d)
    rows = [json.loads(x) for x in (d / ds.TASKS).read_text(encoding="utf-8").splitlines()]
    for r in rows:
        if r["task_id"] == "t1":
            r["q"] = "바뀜"
    _write_tasks(d, rows)
    code, lines = _run("verify-seal", "--out", str(d))
    assert code == 1
    assert any("test_sha256" in x for x in lines) and any("ADR-0010" in x for x in lines)


def test_world_change_mismatch(d: Path) -> None:
    _seal(d)
    (d / ds.WORLD).write_text('{"accounts": {"a": {}}}', encoding="utf-8")
    code, lines = _run("verify-seal", "--out", str(d))
    assert code == 1 and any("world_sha256" in x for x in lines)


def test_dev_change_only_tasks_hash(d: Path) -> None:
    _seal(d)
    rows = [json.loads(x) for x in (d / ds.TASKS).read_text(encoding="utf-8").splitlines()]
    for r in rows:
        if r["split"] == "dev":
            r["q"] = "바뀜"
    _write_tasks(d, rows)
    code, lines = _run("verify-seal", "--out", str(d))
    assert code == 1
    assert any("tasks_sha256" in x for x in lines)
    assert not any("test_sha256" in x for x in lines)


def test_seal_keeps_other_manifest_keys(d: Path) -> None:
    m = Manifest.model_validate_json((d / ds.MANIFEST).read_text(encoding="utf-8"))
    ds.write_manifest(d, m.model_copy(update={"notes": "메모", "details": {"a": [1, {"b": 2}]}}))
    before = _manifest(d)
    assert _seal(d) == 0
    after = _manifest(d)
    assert after.pop("seal")
    assert after == before


@pytest.mark.parametrize("note", ["", "   "])
def test_blank_note_refused(d: Path, note: str) -> None:
    assert _run("seal", "--out", str(d), "--review-note", note)[0] == 2
    assert _manifest(d).get("seal") is None


@pytest.mark.parametrize("name", [ds.WORLD, ds.TASKS, "graph_gold.json", "skeleton.json"])
def test_verify_missing_input(d: Path, name: str) -> None:
    _seal(d)
    (d / name).unlink()
    code, lines = _run("verify-seal", "--out", str(d))
    assert code == 1 and any(f"{name}이 없거나 읽을 수 없다" in x for x in lines)


BAD_TASKS = ["not json\n", '{"task_id": "t1", "split": "test"}\n', "[1]\n"]


@pytest.mark.parametrize("text", BAD_TASKS)
def test_bad_tasks_refused_on_seal(d: Path, text: str) -> None:
    (d / ds.TASKS).write_text(text, encoding="utf-8")
    assert _seal(d) == 2
    assert _manifest(d).get("seal") is None


@pytest.mark.parametrize("text", BAD_TASKS)
def test_bad_tasks_mismatch_on_verify(d: Path, text: str) -> None:
    _seal(d)
    (d / ds.TASKS).write_text(text, encoding="utf-8")
    code, lines = _run("verify-seal", "--out", str(d))
    assert code == 1 and any(ds.TASKS in x for x in lines)
