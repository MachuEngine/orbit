import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orbit.evaldata.generate import git_sha as current_git_sha
from orbit.evaldata.v1 import search_templates
from orbit.evaldata.v1.facts import norm as normalize
from orbit.evaldata.v1.search_set import (
    SearchSetError,
    assign_splits,
    generate,
    load_search_set,
)
from orbit.evaldata.v1.skeleton import SearchTopicTruth, Skeleton

ROOT = Path(__file__).resolve().parents[5]
DATA = ROOT / "eval" / "datasets" / "orbit-eval" / "v1"


def _mutable_dataset(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = tmp_path / "dataset"
    root.mkdir()
    skeleton = json.loads((DATA / "skeleton.json").read_text(encoding="utf-8"))
    world = json.loads((DATA / "world.json").read_text(encoding="utf-8"))
    return root, skeleton, world


def _save_dataset(root: Path, skeleton: dict[str, Any], world: dict[str, Any]) -> None:
    (root / "skeleton.json").write_text(json.dumps(skeleton), encoding="utf-8")
    (root / "world.json").write_text(json.dumps(world), encoding="utf-8")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(150):
        rows.append(
            {
                "query_id": f"q-{index}",
                "query": f"distinct query {index}",
                "scenario_id": f"scenario-{index}",
                "split": "dev" if index < 75 else "test",
                "cluster_id": f"cluster-{index}",
                "as_of": "2026-11-01T00:00:00Z",
                "form": ("keyword", "question", "person_time")[index % 3],
                "mailboxes": {
                    size: {"account_id": f"account-{size}", "relevance": {f"id-{index}": 2}}
                    for size in ("200", "1000", "5000")
                },
                "body_refs": {f"body-{index}": 2},
            }
        )
    return rows


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_actual_world_generates_180_queries_and_complete_qrels() -> None:
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert len(rows) == 180
    assert manifest["scenario_count"] == 60
    assert manifest["missing_grade1_topics"] == 0
    assert set(r["form"] for r in rows) == {"keyword", "question", "person_time"}
    assert all(set(r["mailboxes"]) == {"200", "1000", "5000"} for r in rows)
    assert all(2 in box["relevance"].values() for r in rows for box in r["mailboxes"].values())
    assert any(0 in box["relevance"].values() for r in rows for box in r["mailboxes"].values())
    assert all(r["query"] == r["query"].strip() for r in rows)
    assert normalize("A  B") == normalize("ab")
    assert sum(manifest["counts_by_split_form"].values()) == len(rows)
    skeleton = Skeleton.model_validate_json((DATA / "skeleton.json").read_bytes())
    truths = {
        scenario.scenario_id: SearchTopicTruth.model_validate(scenario.truth)
        for scenario in skeleton.scenarios
        if scenario.type == "search_topic"
    }
    for row in rows:
        truth = truths[row["scenario_id"]]
        assert normalize(truth.topic_phrase) in normalize(row["query"])
        assert (
            sum(normalize(t.topic_phrase) in normalize(row["query"]) for t in truths.values()) == 1
        )
        assert all(
            normalize(surface) not in normalize(row["query"])
            for surface in truth.fact.surface_forms
        )


def test_search_generation_and_saved_loader_are_deterministic(tmp_path: Path) -> None:
    first, manifest = generate(DATA, 20261116, "test-sha")
    second, manifest2 = generate(DATA, 20261116, "test-sha")
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )
    assert manifest == manifest2
    path = tmp_path / "search.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in first), encoding="utf-8"
    )
    manifest_path = tmp_path / "search-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert load_search_set(path, manifest_path) == first


def test_cli_output_bytes_repeat(tmp_path: Path) -> None:
    outputs = [tmp_path / "one", tmp_path / "two"]
    protected_hashes = {
        name: _hash(DATA / name) for name in ("world.json", "skeleton.json", "tasks.jsonl")
    }
    for out in outputs:
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "orbit.evaldata.v1.search_set",
                "--dataset-dir",
                str(DATA),
                "--seed",
                "20261116",
                "--out",
                str(out),
            ],
            cwd=ROOT / "backend",
            check=True,
            capture_output=True,
            text=True,
        )
    assert (outputs[0] / "search.jsonl").read_bytes() == (outputs[1] / "search.jsonl").read_bytes()
    assert (outputs[0] / "search-manifest.json").read_bytes() == (
        outputs[1] / "search-manifest.json"
    ).read_bytes()
    manifest = json.loads((outputs[0] / "search-manifest.json").read_bytes())
    assert manifest["generator_git_sha"] == current_git_sha()
    assert protected_hashes == {
        name: _hash(DATA / name) for name in ("world.json", "skeleton.json", "tasks.jsonl")
    }


def test_small_mailbox_grade_two_body_text_must_validate(tmp_path: Path) -> None:
    root, skeleton, world = _mutable_dataset(tmp_path)
    topic = next(s for s in skeleton["scenarios"] if s["type"] == "search_topic")
    truth = topic["truth"]
    body = next(b for b, grade in truth["relevance_by_body"].items() if grade == 2)
    kind = truth["artifact_by_body"][body]
    box = next(v for v in skeleton["search_mailboxes"].values() if v["size"] == 200)
    mapping = box["message_by_body"] if kind == "mail" else box["file_by_body"]
    account_id = next(k for k, v in skeleton["search_mailboxes"].items() if v is box)
    collection_name, text_key = ("messages", "body_text") if kind == "mail" else ("files", "text")
    item = next(
        x for x in world["accounts"][account_id][collection_name] if x["id"] == mapping[body]
    )
    item[text_key] = "deliberately corrupted body"
    _save_dataset(root, skeleton, world)
    with pytest.raises(SearchSetError, match="lacks fact text"):
        generate(root, 20261116, "test-sha")


def test_swapped_body_to_artifact_mapping_must_validate(tmp_path: Path) -> None:
    root, skeleton, world = _mutable_dataset(tmp_path)
    box = next(v for v in skeleton["search_mailboxes"].values() if v["size"] == 5000)
    body_ids = list(box["message_by_body"])[:2]
    box["message_by_body"][body_ids[0]], box["message_by_body"][body_ids[1]] = (
        box["message_by_body"][body_ids[1]],
        box["message_by_body"][body_ids[0]],
    )
    _save_dataset(root, skeleton, world)
    with pytest.raises(SearchSetError, match="body mapping does not match skeleton"):
        generate(root, 20261116, "test-sha")


def test_small_mailbox_body_subset_corruption_must_validate(tmp_path: Path) -> None:
    root, skeleton, world = _mutable_dataset(tmp_path)
    box = next(v for v in skeleton["search_mailboxes"].values() if v["size"] == 200)
    del box["message_by_body"][next(iter(box["message_by_body"]))]
    _save_dataset(root, skeleton, world)
    with pytest.raises(SearchSetError, match="body subset|mapping does not match"):
        generate(root, 20261116, "test-sha")


def test_topic_without_grade_one_fails_with_count(tmp_path: Path) -> None:
    root, skeleton, world = _mutable_dataset(tmp_path)
    scenario = next(s for s in skeleton["scenarios"] if s["type"] == "search_topic")
    relevance = scenario["truth"]["relevance_by_body"]
    body = next(b for b, grade in relevance.items() if grade == 1)
    relevance[body] = 0
    _save_dataset(root, skeleton, world)
    with pytest.raises(SearchSetError, match="1 search topics have no grade-1 body"):
        generate(root, 20261116, "test-sha")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda r: r[0].update(split="holdout"), "split"),
        (lambda r: r[0].update(as_of="2026-11-01T00:00:00"), "aware"),
        (lambda r: r[0]["body_refs"].update({"bad": 3}), "body_refs"),
        (lambda r: r[0]["mailboxes"]["200"]["relevance"].update({"bad": True}), "qrel grade"),
        (lambda r: r[1].update(query="DISTINCT   QUERY 0"), "normalized query"),
    ],
)
def test_loader_rejects_invalid_fields(tmp_path: Path, mutation: Any, message: str) -> None:
    rows = _synthetic_rows()
    mutation(rows)
    path = tmp_path / "invalid.jsonl"
    _write_rows(path, rows)
    with pytest.raises(SearchSetError, match=message):
        load_search_set(path)


@pytest.mark.parametrize("shared_key", ["scenario", "body"])
def test_loader_rejects_shared_evidence_across_cluster_splits(
    tmp_path: Path, shared_key: str
) -> None:
    rows = _synthetic_rows()
    if shared_key == "scenario":
        rows[1]["scenario_id"] = rows[0]["scenario_id"]
    else:
        rows[1]["body_refs"] = rows[0]["body_refs"]
    rows[1]["split"] = "test"
    path = tmp_path / "shared.jsonl"
    _write_rows(path, rows)
    with pytest.raises(SearchSetError, match="multiple cluster labels"):
        load_search_set(path)
    rows[1]["cluster_id"] = rows[0]["cluster_id"]
    with pytest.raises(SearchSetError, match="multiple splits"):
        _write_rows(path, rows)
        load_search_set(path)


def test_loader_rejects_transitive_body_component_with_split_labels(tmp_path: Path) -> None:
    rows = _synthetic_rows()
    rows[0]["body_refs"] = {"shared-a": 2}
    rows[1]["body_refs"] = {"shared-a": 0, "shared-b": 1}
    rows[2]["body_refs"] = {"shared-b": 1}
    path = tmp_path / "transitive.jsonl"
    _write_rows(path, rows)
    with pytest.raises(SearchSetError, match="multiple cluster labels"):
        load_search_set(path)


def test_loader_checks_manifest_cluster_counts(tmp_path: Path) -> None:
    rows = _synthetic_rows()
    path = tmp_path / "search.jsonl"
    _write_rows(path, rows)
    form_counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['split']}:{row['form']}"
        form_counts[key] = form_counts.get(key, 0) + 1
    manifest: dict[str, Any] = {
        "query_count": 150,
        "counts_by_split_form": form_counts,
        "cluster_count": 150,
        "clusters_by_split": {"dev": 75, "test": 75},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert len(load_search_set(path, manifest_path)) == 150
    manifest["clusters_by_split"]["dev"] = 74
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SearchSetError, match="clusters_by_split"):
        load_search_set(path, manifest_path)


def test_shared_bodies_form_transitive_topic_cluster(tmp_path: Path) -> None:
    root, skeleton, world = _mutable_dataset(tmp_path)
    topics = [s for s in skeleton["scenarios"] if s["type"] == "search_topic"]
    first, second, third = topics[:3]
    first_body = next(iter(first["truth"]["relevance_by_body"]))
    second_body = next(iter(second["truth"]["relevance_by_body"]))
    for target, body, source in (
        (second, first_body, first),
        (third, second_body, second),
    ):
        target["truth"]["relevance_by_body"][body] = 0
        target["truth"]["artifact_by_body"][body] = source["truth"]["artifact_by_body"][body]
    _save_dataset(root, skeleton, world)
    rows, _ = generate(root, 20261116, "test-sha")
    cluster_ids = {
        row["cluster_id"]
        for row in rows
        if row["scenario_id"] in {first["scenario_id"], second["scenario_id"], third["scenario_id"]}
    }
    assert len(cluster_ids) == 1


def test_colliding_full_topic_phrases_are_dropped_with_reason(tmp_path: Path) -> None:
    root, skeleton, world = _mutable_dataset(tmp_path)
    topics = [s for s in skeleton["scenarios"] if s["type"] == "search_topic"]
    topics[1]["truth"]["topic_phrase"] = topics[0]["truth"]["topic_phrase"]
    _save_dataset(root, skeleton, world)
    rows, manifest = generate(root, 20261116, "test-sha")
    assert len(rows) == 174
    assert manifest["dropped"]["ambiguous_topic_phrase"] == 6


def test_answer_leakage_and_normalized_query_duplicates_are_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "sc-search-topic-0001"
    original = search_templates.render
    skeleton_model = Skeleton.model_validate_json((DATA / "skeleton.json").read_bytes())
    scenario = next(s for s in skeleton_model.scenarios if s.scenario_id == target)
    target_truth = SearchTopicTruth.model_validate(scenario.truth)
    target_phrase = target_truth.topic_phrase
    target_fact = target_truth.fact.surface_forms[0]

    def leak(
        phrase: str, form: str, author: str, when: Any, kind: str, fact_kind: str, rng: Any
    ) -> str:
        if phrase == target_phrase:
            return original(phrase, form, author, when, kind, fact_kind, rng) + " " + target_fact
        return original(phrase, form, author, when, kind, fact_kind, rng)

    monkeypatch.setattr(search_templates, "render", leak)
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert len(rows) == 177
    assert manifest["dropped"]["answer_fact_leakage"] == 3
    monkeypatch.setattr(search_templates, "render", original)

    phrase = target_phrase

    def duplicate(
        topic_phrase: str, form: str, author: str, when: Any, kind: str, fact_kind: str, rng: Any
    ) -> str:
        if topic_phrase == phrase:
            return f"{topic_phrase} 최종 금액"
        return original(topic_phrase, form, author, when, kind, fact_kind, rng)

    monkeypatch.setattr(search_templates, "render", duplicate)
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert len(rows) == 177
    assert manifest["dropped"]["duplicate_query"] == 2
    assert manifest["dropped"]["insufficient_forms"] == 1
    retained: dict[str, int] = {}
    for row in rows:
        retained[row["scenario_id"]] = retained.get(row["scenario_id"], 0) + 1
    assert all(2 <= count <= 4 for count in retained.values())


@pytest.mark.parametrize("raw_row", ["[]", "null", '"text"'])
def test_loader_rejects_non_object_rows(tmp_path: Path, raw_row: str) -> None:
    path = tmp_path / "bad-row.jsonl"
    path.write_text(raw_row + "\n", encoding="utf-8")
    with pytest.raises(SearchSetError, match="row must be an object"):
        load_search_set(path)


@pytest.mark.parametrize("mailboxes", [None, []])
def test_loader_rejects_non_object_mailboxes(tmp_path: Path, mailboxes: Any) -> None:
    rows = _synthetic_rows()
    rows[0]["mailboxes"] = mailboxes
    path = tmp_path / "bad-mailboxes.jsonl"
    _write_rows(path, rows)
    with pytest.raises(SearchSetError, match="mailboxes must be an object"):
        load_search_set(path)


def test_loader_rejects_non_object_manifest(tmp_path: Path) -> None:
    path = tmp_path / "search.jsonl"
    _write_rows(path, _synthetic_rows())
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(SearchSetError, match="manifest must be an object"):
        load_search_set(path, manifest_path)


def test_split_assignment_keeps_transitive_cluster_unit() -> None:
    rows = [
        {"cluster_id": "a", "query_id": "a-1"},
        {"cluster_id": "a", "query_id": "a-2"},
        {"cluster_id": "b", "query_id": "b-1"},
        {"cluster_id": "c", "query_id": "c-1"},
    ]
    assign_splits(rows, 10)
    assert len({r["split"] for r in rows if r["cluster_id"] == "a"}) == 1


def test_split_assignment_rejects_one_connected_component() -> None:
    rows = [{"cluster_id": "all", "query_id": f"q-{i}"} for i in range(180)]
    with pytest.raises(SearchSetError, match="usable dev/test split"):
        assign_splits(rows, 10)


def test_loader_rejects_duplicates_and_mixed_cluster(tmp_path: Path) -> None:
    row = {
        "query_id": "same",
        "query": "unique query",
        "scenario_id": "s",
        "as_of": "2026-11-01T00:00:00Z",
        "form": "keyword",
        "cluster_id": "c",
        "split": "dev",
        "body_refs": {},
        "mailboxes": {
            size: {"account_id": f"acct-{size}", "relevance": {}}
            for size in ("200", "1000", "5000")
        },
    }
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(SearchSetError, match="duplicate"):
        load_search_set(path)
    row["query_id"] = "second"
    row["split"] = "test"
    path.write_text(
        json.dumps({**row, "query_id": "first", "query": "first query", "split": "dev"})
        + "\n"
        + json.dumps({**row, "query": "second query"})
        + "\n"
    )
    with pytest.raises(SearchSetError, match="cluster"):
        load_search_set(path)
