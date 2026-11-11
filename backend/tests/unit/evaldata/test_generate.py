"""생성기 산출물의 결정성, 규모, 분할, 합성 데이터 규칙."""

import json
import re
from collections import Counter
from pathlib import Path

from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.dataset import load_dataset
from orbit.evaldata.generate import generate
from orbit.evaldata.schema import Gold, Manifest, Task
from orbit.evaldata.timeutil import time_forms

from .conftest import COMMITTED_DATASET, SEED, SHA, WORLD_MODEL

FILES = ["manifest.json", "world.json", "tasks.jsonl"]


def _read(path: Path) -> dict[str, bytes]:
    return {name: (path / name).read_bytes() for name in FILES}


def test_same_seed_is_byte_identical(dataset_dir: Path, tmp_path: Path) -> None:
    generate(WORLD_MODEL, SEED, tmp_path / "again", SHA)
    assert _read(tmp_path / "again") == _read(dataset_dir)


def test_different_seed_differs(dataset_dir: Path, tmp_path: Path) -> None:
    generate(WORLD_MODEL, SEED + 1, tmp_path / "other", SHA)
    other, base = _read(tmp_path / "other"), _read(dataset_dir)
    assert other["world.json"] != base["world.json"]
    assert other["tasks.jsonl"] != base["tasks.jsonl"]


def test_committed_dataset_is_reproducible(tmp_path: Path) -> None:
    """저장소에 커밋된 데이터셋은 manifest의 시드·SHA로 다시 만든 결과와 같아야 한다."""
    manifest = Manifest.model_validate_json(
        (COMMITTED_DATASET / "manifest.json").read_text("utf-8")
    )
    generate(WORLD_MODEL, manifest.seed, tmp_path / "re", manifest.generator_git_sha)
    for name in ("world.json", "tasks.jsonl"):
        assert (tmp_path / "re" / name).read_bytes() == (COMMITTED_DATASET / name).read_bytes()
    assert load_dataset(COMMITTED_DATASET, include_excluded=True).tasks


def test_scale_and_counts(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    c = ds.manifest.counts
    assert c.users == 2
    assert 180 <= c.messages <= 240
    assert 55 <= c.events <= 90
    assert c.tasks == 60 == len(ds.tasks)
    per_type = Counter(t.tags[0] for t in ds.tasks)
    assert per_type == {
        "type:day_schedule": 10,
        "type:free_slot": 10,
        "type:reschedule": 12,
        "type:cancelled_exclusion": 8,
        "type:project_week": 10,
        "type:person_next": 10,
    }
    assert ds.manifest.created_with == "templates"
    assert ds.manifest.split_ratio.dev == 0.6
    assert ds.manifest.split_ratio.test == 0.4
    assert ds.manifest.generator_git_sha == SHA


def test_world_loads_in_sim(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    world = SimWorld.load(dataset_dir / "world.json", FixedClock(ds.tasks[0].as_of_time))
    assert sorted(world.account_ids) == sorted(ds.world_dict["accounts"])
    for acct in ds.world_dict["accounts"].values():
        statuses = {e["status"] for e in acct["events"]}
        assert statuses == {"confirmed", "cancelled"}


def test_no_split_leakage(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    splits: dict[str, set[str]] = {}
    for t in ds.tasks:
        splits.setdefault(t.scenario_id, set()).add(t.split)
    assert all(len(s) == 1 for s in splits.values())
    counts = Counter(t.split for t in ds.tasks)
    assert 30 <= counts["dev"] <= 42
    assert counts["test"] >= 18
    # 유형마다 dev와 test에 모두 과제가 있다
    by_type = {(t.tags[0], t.split) for t in ds.tasks}
    assert len(by_type) == 12


def test_task_contract_fields() -> None:
    assert list(Task.model_fields) == [
        "task_id",
        "scenario_id",
        "split",
        "as_of_time",
        "account_id",
        "turns",
        "gold",
        "difficulty",
        "tags",
    ]
    assert list(Gold.model_fields) == [
        "answer_facts",
        "must_not_contain",
        "expected_changes",
        "required_tool_calls",
        "forbidden_tools",
    ]


def test_read_only_gold(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    write_tools = {
        "create_event",
        "update_event",
        "cancel_event",
        "create_draft",
        "send_draft",
        "delete_draft",
    }
    for t in ds.tasks:
        g = t.gold
        assert g.expected_changes == []
        assert write_tools <= set(g.forbidden_tools)
        assert g.required_tool_calls
        assert g.answer_facts
        assert t.as_of_time.utcoffset() is not None
        assert t.turns[0].role == "user"
        for text in g.must_not_contain:
            # 정답 표현에 금지 표현이 섞이면 만점 답이 실패한다
            assert not any(text in form for f in g.answer_facts for form in f.any_of)


def test_cancelled_tasks_forbid_cancelled_titles(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    cancelled = [t for t in ds.tasks if t.tags[0] == "type:cancelled_exclusion"]
    assert cancelled
    assert all(t.gold.must_not_contain for t in cancelled)


def test_synthetic_data_only(dataset_dir: Path) -> None:
    allowed = re.compile(r"@([\w.-]+\.)?(example\.(com|org|net)|test)$")
    text = (dataset_dir / "world.json").read_text("utf-8")
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+", text))
    assert emails
    assert all(allowed.search(e) for e in emails), sorted(emails)
    assert "http://" not in text and "https://" not in text


def test_homonym_pair_present(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    people = {
        a["email"]: a["name"]
        for acct in ds.world_dict["accounts"].values()
        for m in acct["messages"]
        for a in [m["from"]]
    }
    names = Counter(people.values())
    assert any(n == 2 for n in names.values())
    assert any(int(t.difficulty.get("homonym", 0)) == 1 for t in ds.tasks)


def test_surface_forms() -> None:
    from datetime import datetime

    from orbit.evaldata.timeutil import KST, date_forms

    t = datetime(2026, 11, 17, 14, 0, tzinfo=KST)
    assert {"14:00", "오후 2시", "14시"} <= set(time_forms(t))
    assert "오후 2시 30분" in time_forms(t.replace(minute=30))
    assert {"11월 17일", "11/17"} <= set(date_forms(t.date()))


def test_manifest_json_is_valid(dataset_dir: Path) -> None:
    data = json.loads((dataset_dir / "manifest.json").read_text("utf-8"))
    assert set(data) == {
        "name",
        "version",
        "generator_git_sha",
        "seed",
        "created_with",
        "split_ratio",
        "counts",
        "notes",
    }
