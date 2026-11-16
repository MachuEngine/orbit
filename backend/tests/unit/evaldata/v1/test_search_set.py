import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orbit.evaldata.generate import git_sha as current_git_sha
from orbit.evaldata.v1 import search_templates
from orbit.evaldata.v1.facts import norm as normalize
from orbit.evaldata.v1.search_set import (
    BASELINE_SPLIT_SEED,
    FROZEN_CONTRACT_SHA256,
    SearchSetError,
    _choose_person_time,
    _deduplicate_queries,
    _unique_time_precision,
    assign_splits,
    generate,
    load_search_set,
)
from orbit.evaldata.v1.skeleton import SearchTopicTruth, Skeleton
from orbit.evaldata.v1.task_world import load_task_world
from orbit.evaldata.v1.templates import SEARCH_NOUNS

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
                "form": ("keyword", "paraphrase_question", "person_time")[index % 3],
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
    assert set(r["form"] for r in rows) == {"keyword", "paraphrase_question", "person_time"}
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
        if row["form"] == "keyword":
            assert normalize(truth.topic_phrase) in normalize(row["query"])
        else:
            assert normalize(truth.topic_phrase) not in normalize(row["query"])
            noun = truth.topic_phrase.split(" ", 1)[1]
            outside_project = normalize(row["query"]).replace(
                normalize(truth.topic_phrase.split(" ", 1)[0]), ""
            )
            assert all(normalize(token) not in outside_project for token in noun.split())
        assert all(
            normalize(surface) not in normalize(row["query"])
            for surface in truth.fact.surface_forms
        )


def test_frozen_contract_hash_and_partition_ignore_wording_seed() -> None:
    contract_hashes = []
    splits = []
    for seed in (20261116, 1001):
        rows, manifest = generate(DATA, seed, "test-sha")
        contract: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = {
                key: row[key] for key in ("split", "cluster_id", "mailboxes", "body_refs", "as_of")
            }
            if row["scenario_id"] in contract:
                assert contract[row["scenario_id"]] == value
            else:
                contract[row["scenario_id"]] = value
        encoded = json.dumps(
            contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        contract_hashes.append(hashlib.sha256(encoded).hexdigest())
        splits.append({sid: values["split"] for sid, values in contract.items()})
        assert manifest["split_seed"] == BASELINE_SPLIT_SEED
    assert contract_hashes == [FROZEN_CONTRACT_SHA256, FROZEN_CONTRACT_SHA256]
    assert splits[0] == splits[1]


def test_synonym_coverage_and_actual_person_time_refinement() -> None:
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert manifest["synonym_noun_count"] == 20
    assert manifest["synonym_variant_count"] >= 40
    assert manifest["person_time_precision_counts"] == {"date": 41, "month": 19}
    assert len(rows) == 180
    assert set(search_templates.NOUN_SYNONYMS) == set(SEARCH_NOUNS)
    for noun, aliases in search_templates.NOUN_SYNONYMS.items():
        assert len({normalize(alias) for alias in aliases}) >= 2
        assert all(
            normalize(token) not in normalize(alias) for token in noun.split() for alias in aliases
        )


def test_keyword_queries_match_lexical_baseline() -> None:
    baseline = [
        json.loads(line)
        for line in (DATA / "search.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows, _ = generate(DATA, 20261116, "test-sha")
    old_keywords = {
        row["scenario_id"]: row["query"] for row in baseline if row["form"] == "keyword"
    }
    new_keywords = {row["scenario_id"]: row["query"] for row in rows if row["form"] == "keyword"}
    assert old_keywords == new_keywords


def test_lexical_overlap_statistics_are_hand_calculable() -> None:
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert _test_overlap("달빛정원, 달빛정원! x", "달빛정원 B") == 2 / 3
    phrases = {
        scenario.scenario_id: SearchTopicTruth.model_validate(scenario.truth).topic_phrase
        for scenario in Skeleton.model_validate_json(
            (DATA / "skeleton.json").read_bytes()
        ).scenarios
        if scenario.type == "search_topic"
    }
    for form in ("keyword", "paraphrase_question", "person_time"):
        form_rows = [row for row in rows if row["form"] == form]
        values = [_test_overlap(row["query"], phrases[row["scenario_id"]]) for row in form_rows]
        assert manifest["lexical_overlap_by_form"][form] == {
            "count": len(values),
            "mean": round(sum(values) / len(values), 8),
            "max": round(max(values), 8),
        }


def _test_overlap(query: str, phrase: str) -> float:
    """Independent hand oracle: NFC/casefold then Unicode word tokens, punctuation split."""
    query_tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", query).casefold())
    topic_tokens = set(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", phrase).casefold()))
    return sum(token in topic_tokens for token in query_tokens) / len(query_tokens)


def test_time_uniqueness_uses_source_conditions_not_wording() -> None:
    fact = SimpleNamespace(kind="amount")
    first = {
        "project": "달빛정원",
        "author": "전준혁",
        "truth": SimpleNamespace(fact=fact),
        "when": dt.datetime(2026, 10, 14, 14, 15, tzinfo=dt.UTC),
        "wording": "최신 안내 자료",
    }
    same_month = {**first, "when": dt.datetime(2026, 10, 29, 14, 40, tzinfo=dt.UTC)}
    assert _unique_time_precision(first, [first, same_month]) == "date"
    same_date = {**first, "when": dt.datetime(2026, 10, 14, 19, 55, tzinfo=dt.UTC)}
    assert _unique_time_precision(first, [first, same_date]) == "minute"
    same_minute = {**first}
    assert _unique_time_precision(first, [first, same_minute]) is None


def test_ambiguity_drop_limit_is_one_per_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orbit.evaldata.v1.search_set as search_set

    target = "sc-search-topic-0001"
    original = search_set._unique_time_precision

    def ambiguous_one(context: dict[str, Any], contexts: list[dict[str, Any]]) -> str | None:
        return None if context["scenario_id"] == target else original(context, contexts)

    monkeypatch.setattr(search_set, "_unique_time_precision", ambiguous_one)
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert len(rows) == 179
    assert manifest["dropped"]["ambiguous_person_time"] == 1
    assert manifest["dropped_by_topic"] == {target: 1}

    skeleton = Skeleton.model_validate_json((DATA / "skeleton.json").read_bytes())
    scenario = next(s for s in skeleton.scenarios if s.scenario_id == target)
    noun = SearchTopicTruth.model_validate(scenario.truth).topic_phrase.split(" ", 1)[1]
    aliases = ("unique phrase alpha", "unique phrase beta")
    synonyms = dict(search_templates.NOUN_SYNONYMS)
    synonyms[noun] = aliases
    synonyms["계약 갱신"] = (*synonyms["계약 갱신"], aliases[0])
    monkeypatch.setattr(search_templates, "NOUN_SYNONYMS", synonyms)
    monkeypatch.setattr(search_set, "_unique_time_precision", original)
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert len(rows) == 180
    assert "ambiguous_synonym" not in manifest["dropped"]

    synonyms = dict(search_templates.NOUN_SYNONYMS)
    synonyms[noun] = aliases
    synonyms["출시 일정"] = (*synonyms["출시 일정"], *aliases)
    monkeypatch.setattr(search_templates, "NOUN_SYNONYMS", synonyms)
    monkeypatch.setattr(search_set, "_unique_time_precision", ambiguous_one)
    with pytest.raises(SearchSetError, match="more than one query dropped"):
        generate(DATA, 20261116, "test-sha")


def test_duplicate_normalized_synonyms_fail_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    synonyms = dict(search_templates.NOUN_SYNONYMS)
    synonyms["견적"] = ("같은 표현", "같은 표현")
    monkeypatch.setattr(search_templates, "NOUN_SYNONYMS", synonyms)
    with pytest.raises(SearchSetError, match="at least two synonyms"):
        generate(DATA, 20261116, "test-sha")


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
        "split_seed": BASELINE_SPLIT_SEED,
        "synonym_noun_count": 20,
        "synonym_variant_count": 40,
        "dropped_by_topic": {},
        "lexical_overlap_by_form": {
            form: {"count": 50, "mean": 0.5, "max": 0.5}
            for form in ("keyword", "paraphrase_question", "person_time")
        },
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
    with pytest.raises(SearchSetError, match="exact project prefix"):
        generate(root, 20261116, "test-sha")


def test_answer_leakage_and_duplicate_queries_are_counted(
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
        phrase: str,
        form: str,
        author: str,
        when: Any,
        kind: str,
        fact_kind: str,
        rng: Any,
        **kwargs: Any,
    ) -> str:
        if phrase == target_phrase:
            return (
                original(phrase, form, author, when, kind, fact_kind, rng, **kwargs)
                + " "
                + target_fact
            )
        return original(phrase, form, author, when, kind, fact_kind, rng, **kwargs)

    monkeypatch.setattr(search_templates, "render", leak)
    with pytest.raises(SearchSetError, match="answer fact leaked"):
        generate(DATA, 20261116, "test-sha")
    monkeypatch.setattr(search_templates, "render", original)

    duplicate_rows = [
        {"query": "Alpha   beta", "scenario_id": "first"},
        {"query": "alpha beta", "scenario_id": "second"},
    ]
    dropped: Counter[str] = Counter()
    per_topic: Counter[str] = Counter()
    unique = _deduplicate_queries(duplicate_rows, dropped, per_topic)
    assert unique == duplicate_rows[:1]
    assert dropped["duplicate_query"] == 1
    assert per_topic["second"] == 1


@pytest.mark.parametrize(
    ("form", "field", "scenario_id"),
    [
        ("keyword", "topic_phrase", "sc-search-topic-0001"),
        ("paraphrase_question", "project", "sc-search-topic-0001"),
        ("paraphrase_question", "synonym", "sc-search-topic-0001"),
        ("paraphrase_question", "fact_kind", "sc-search-topic-0001"),
        ("person_time", "project", "sc-search-topic-0001"),
        ("person_time", "author", "sc-search-topic-0001"),
        ("person_time", "source_time", "sc-search-topic-0001"),
        ("person_time", "fact_kind", "sc-search-topic-0001"),
        ("person_time", "source_time", "sc-search-topic-0005"),
    ],
)
def test_renderer_cannot_omit_required_form_fields(
    monkeypatch: pytest.MonkeyPatch, form: str, field: str, scenario_id: str
) -> None:
    original = search_templates.render
    skeleton_model = Skeleton.model_validate_json((DATA / "skeleton.json").read_bytes())
    target_scenario = next(
        scenario for scenario in skeleton_model.scenarios if scenario.scenario_id == scenario_id
    )
    target_phrase = SearchTopicTruth.model_validate(target_scenario.truth).topic_phrase

    def omit(
        phrase: str,
        current_form: str,
        author: str,
        when: Any,
        artifact_kind: str,
        fact_kind: str,
        rng: Any,
        **kwargs: Any,
    ) -> str:
        query = original(
            phrase, current_form, author, when, artifact_kind, fact_kind, rng, **kwargs
        )
        if current_form != form or phrase != target_phrase:
            return query
        required = {
            "topic_phrase": phrase,
            "project": kwargs["project"],
            "synonym": kwargs["synonym"],
            "fact_kind": search_templates.fact_cue(fact_kind),
            "author": author,
            "source_time": search_templates.format_source_time(when, kwargs["time_precision"]),
        }[field]
        return query.replace(required, "", 1)

    monkeypatch.setattr(search_templates, "render", omit)
    with pytest.raises(SearchSetError, match=f"{form} query is missing required {field}"):
        generate(DATA, 20261116, "test-sha")


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


@pytest.mark.parametrize(
    ("word", "pair", "expected"),
    [
        ("2026년 10월 28일", "이야/야", "2026년 10월 28일이야"),
        ("2026년 11월", "이야/야", "2026년 11월이야"),
        ("최세아", "이야/야", "최세아야"),
        ("최종 금액", "이/가", "최종 금액이"),
        ("확정 마감", "을/를", "확정 마감을"),
        ("서아린님", "이/가", "서아린님이"),
        ("프로젝트", "이/가", "프로젝트가"),
        ("프로젝트", "은/는", "프로젝트는"),
        ("마감일", "은/는", "마감일은"),
        ("달빛정원", "과/와", "달빛정원과"),
        ("고지원", "과/와", "고지원과"),
        ("고지원", "을/를", "고지원을"),
        ("ABC 12", "이/가", "ABC 12가"),
    ],
)
def test_josa_follows_final_consonant(word: str, pair: str, expected: str) -> None:
    assert search_templates.attach_josa(word, pair) == expected


def test_final_consonant_uses_last_hangul_for_digit_endings() -> None:
    assert search_templates.has_final_consonant("28일")
    assert search_templates.has_final_consonant("11월")
    assert not search_templates.has_final_consonant("3시 5분 7초 2")
    assert not search_templates.has_final_consonant("")
    assert not search_templates.has_final_consonant("ABC")


def test_generated_queries_have_no_josa_error() -> None:
    rows, _ = generate(DATA, 20261116, "test-sha")
    # A sentence-ending "야" must not directly follow a syllable with a final consonant.
    ending = re.compile(r"([가-힣])야(?=\s|$|[?.!,])")
    checked = 0
    for row in rows:
        for match in ending.finditer(row["query"]):
            checked += 1
            assert not search_templates.has_final_consonant(match.group(1)), row["query"]
    assert checked > 0
    assert not any(re.search(r"[월일시분초]야(?=\s|$)", row["query"]) for row in rows)


def _world_author_when(
    tw: Any, account_id: str, artifact_id: str, kind: str
) -> tuple[str, dt.datetime]:
    account = tw.accounts[account_id]
    if kind == "mail":
        item = account.messages[artifact_id]
        author, raw = item["from"]["name"], item["sent_at"]
    else:
        item = account.files[artifact_id]
        author, raw = item["owners"][0]["name"], item["modified_at"]
    return author, dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(account.tz)


def test_person_time_condition_does_not_match_grade_one_bodies() -> None:
    rows, manifest = generate(DATA, 20261116, "test-sha")
    tw, _ = load_task_world(DATA)
    skeleton = Skeleton.model_validate_json((DATA / "skeleton.json").read_bytes())
    kinds = {
        scenario.scenario_id: SearchTopicTruth.model_validate(scenario.truth).artifact_by_body
        for scenario in skeleton.scenarios
        if scenario.type == "search_topic"
    }
    latest_rows = 0
    for row in (r for r in rows if r["form"] == "person_time"):
        query = row["query"]
        precision = "date" if re.search(r"\d+월 \d+일", query) else "month"
        latest = any(cue in query for cue in search_templates.LATEST_CUES)
        latest_rows += latest
        project = next(
            body.context.project
            for body_id, body in skeleton.bodies.items()
            if body_id in row["body_refs"] and row["body_refs"][body_id] == 2
        )
        assert project in query
        for box in row["mailboxes"].values():
            artifacts = {
                artifact_id: grade for artifact_id, grade in box["relevance"].items() if grade >= 1
            }
            grade2: list[dt.datetime] = []
            grade1: list[tuple[str, dt.datetime]] = []
            for body_id, grade in row["body_refs"].items():
                if grade == 0:
                    continue
                kind = kinds[row["scenario_id"]][body_id]
                size_box = skeleton.search_mailboxes[box["account_id"]]
                mapping = size_box.message_by_body if kind == "mail" else size_box.file_by_body
                author, when = _world_author_when(tw, box["account_id"], mapping[body_id], kind)
                assert mapping[body_id] in artifacts
                if grade == 2:
                    assert author in query
                    grade2.append(when)
                else:
                    grade1.append((author, when))
            fmt = "%Y년 %m월 %d일" if precision == "date" else "%Y년 %m월"
            for author, when in grade1:
                matches = author in query and when.strftime(fmt) in query
                if matches:
                    # Only a strictly older body may share the condition, and only with a cue.
                    assert latest
                    assert all(when < g2 for g2 in grade2)
    assert latest_rows == manifest["person_time_latest_count"]
    assert sum(manifest["person_time_precision_counts"].values()) == 60


def _pt_context(
    when: dt.datetime, grade1: list[dt.datetime], author: str = "전준혁"
) -> dict[str, Any]:
    return {
        "project": "달빛정원",
        "author": author,
        "kind": "mail",
        "truth": SimpleNamespace(fact=SimpleNamespace(kind="amount")),
        "when": when,
        "grade1_by_size": {
            size: [
                {
                    "body_id": f"b{i}",
                    "kind": "mail",
                    "author": author,
                    "when": g,
                    "project": "달빛정원",
                }
                for i, g in enumerate(grade1)
            ]
            for size in ("200", "1000", "5000")
        },
    }


def test_person_time_choice_order_date_then_latest_then_drop() -> None:
    tz = dt.UTC
    base = dt.datetime(2026, 10, 14, 9, 0, tzinfo=tz)
    # Same month but a different day: the date alone separates grade 1 from grade 2.
    ctx = _pt_context(base, [dt.datetime(2026, 10, 2, 9, 0, tzinfo=tz)])
    assert _choose_person_time(ctx, [ctx]) == ("date", False)
    # Different person: no conflict, month stays.
    other = _pt_context(base, [])
    assert _choose_person_time(other, [other]) == ("month", False)
    # Same day, grade 1 older: latest cue separates them.
    ctx = _pt_context(base, [dt.datetime(2026, 10, 14, 8, 0, tzinfo=tz)])
    assert _choose_person_time(ctx, [ctx]) == ("date", True)
    # Same day, grade 1 newer than grade 2: cannot be separated.
    ctx = _pt_context(base, [dt.datetime(2026, 10, 14, 10, 0, tzinfo=tz)])
    assert _choose_person_time(ctx, [ctx]) is None
    # Same instant is not "strictly later".
    ctx = _pt_context(base, [base])
    assert _choose_person_time(ctx, [ctx]) is None


def test_latest_wording_is_rendered_and_required() -> None:
    import random

    when = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.UTC)
    for kind in ("mail", "document"):
        for seed in range(4):
            query = search_templates.render(
                "달빛정원 견적",
                "person_time",
                "전준혁",
                when,
                kind,
                "amount",
                random.Random(seed),  # noqa: S311
                project="달빛정원",
                noun="견적",
                time_precision="date",
                latest=True,
            )
            assert any(cue in query for cue in search_templates.LATEST_CUES)
            assert (
                search_templates.missing_required_field(
                    query,
                    "person_time",
                    "달빛정원",
                    "달빛정원 견적",
                    "amount",
                    author="전준혁",
                    when=when,
                    time_precision="date",
                    latest=True,
                )
                is None
            )
            assert (
                search_templates.missing_required_field(
                    query.replace("마지막으로", "").replace("가장 최근에", ""),
                    "person_time",
                    "달빛정원",
                    "달빛정원 견적",
                    "amount",
                    author="전준혁",
                    when=when,
                    time_precision="date",
                    latest=True,
                )
                == "latest"
            )


def test_grade_one_overlap_drop_is_counted_and_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orbit.evaldata.v1.search_set as search_set

    target = "sc-search-topic-0001"
    original = search_set._choose_person_time

    def undecidable(context: dict[str, Any], contexts: list[dict[str, Any]]) -> Any:
        return None if context["scenario_id"] == target else original(context, contexts)

    monkeypatch.setattr(search_set, "_choose_person_time", undecidable)
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert len(rows) == 179
    assert manifest["dropped"] == {"person_time_matches_grade1": 1}
    assert manifest["dropped_by_topic"] == {target: 1}


def test_query_wording_change_keeps_counts_and_contract() -> None:
    rows, manifest = generate(DATA, 20261116, "test-sha")
    assert len(rows) == 180
    assert Counter(r["form"] for r in rows) == {
        "keyword": 60,
        "paraphrase_question": 60,
        "person_time": 60,
    }
    assert manifest["dropped"] == {}
    saved = [
        json.loads(line)
        for line in (DATA / "search.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    keys = ("query_id", "split", "cluster_id", "mailboxes", "body_refs", "as_of", "scenario_id")
    assert [{k: r[k] for k in keys} for r in rows] == [{k: r[k] for k in keys} for r in saved]
