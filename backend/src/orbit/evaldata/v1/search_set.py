"""Deterministic retrieval query and qrel dataset generation (ADR-0026 §6)."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

from orbit.evaldata.generate import git_sha as current_git_sha
from orbit.evaldata.v1 import search_templates
from orbit.evaldata.v1.facts import norm as normalize
from orbit.evaldata.v1.skeleton import SearchTopicTruth
from orbit.evaldata.v1.task_world import TaskWorldError, load_task_world

FORMS = ("keyword", "question", "person_time")
SIZES = ("200", "1000", "5000")


class SearchSetError(ValueError):
    """Search dataset inputs violate retrieval set invariants."""


def _format(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(k, "")) for k in ("name", "subject", "body_text", "text", "body"))


def generate(
    dataset_dir: Path, seed: int, git_sha: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        tw, world = load_task_world(dataset_dir)
    except (TaskWorldError, ValueError) as exc:
        raise SearchSetError(str(exc)) from exc
    sk = tw.skeleton
    boxes = sk.search_mailboxes
    if len(boxes) != 3 or {str(box.size) for box in boxes.values()} != set(SIZES):
        raise SearchSetError("search_mailboxes must contain sizes 200, 1000, and 5000")
    box_by_size = {str(box.size): box for box in boxes.values()}
    for small, large in (("200", "1000"), ("1000", "5000")):
        if box_by_size[small].subset_of != next(
            (key for key, box in boxes.items() if box.size == int(large)), None
        ):
            raise SearchSetError(f"{small} mailbox subset relationship is invalid")
        small_box, large_box = box_by_size[small], box_by_size[large]
        if not set(small_box.message_by_body) <= set(large_box.message_by_body) or not set(
            small_box.file_by_body
        ) <= set(large_box.file_by_body):
            raise SearchSetError(f"{small} mailbox is not a body subset of {large}")
    for size, box in box_by_size.items():
        account_id = next(key for key, candidate in boxes.items() if candidate is box)
        raw_account = world["accounts"][account_id]
        sk_account = sk.accounts[account_id]
        message_ids = {str(item["id"]) for item in raw_account.get("messages", [])}
        file_ids = {str(item["id"]) for item in raw_account.get("files", [])}
        if (
            len(box.message_by_body) != len(set(box.message_by_body.values()))
            or set(box.message_by_body.values()) != message_ids
        ):
            raise SearchSetError(f"{size} mailbox message mapping does not match world IDs")
        if (
            len(box.file_by_body) != len(set(box.file_by_body.values()))
            or set(box.file_by_body.values()) != file_ids
        ):
            raise SearchSetError(f"{size} mailbox file mapping does not match world IDs")
        if len(message_ids) != box.size:
            raise SearchSetError(f"{size} mailbox declared size does not match world messages")
        if {m.id: m.body_id for m in sk_account.messages} != {
            artifact_id: body_id for body_id, artifact_id in box.message_by_body.items()
        }:
            raise SearchSetError(f"{size} mailbox message body mapping does not match skeleton")
        if {f.id: f.body_id for f in sk_account.files} != {
            artifact_id: body_id for body_id, artifact_id in box.file_by_body.items()
        }:
            raise SearchSetError(f"{size} mailbox file body mapping does not match skeleton")
    body_text: dict[str, dict[str, str]] = {}
    for account_id, box in boxes.items():
        raw_account = world["accounts"][account_id]
        for mapping, collection, text_key in (
            (box.message_by_body, raw_account.get("messages", []), "body_text"),
            (box.file_by_body, raw_account.get("files", []), "text"),
        ):
            by_id = {str(item["id"]): item for item in collection}
            for body_id, artifact_id in mapping.items():
                item = by_id.get(artifact_id)
                if item is not None and item.get(text_key) is not None:
                    body_text.setdefault(body_id, {})[account_id] = str(item[text_key])
    topics = sorted(
        (s for s in sk.scenarios if s.type == "search_topic"), key=lambda s: s.scenario_id
    )
    if len(topics) != 60:
        raise SearchSetError(f"expected 60 search_topic scenarios, got {len(topics)}")
    candidates: list[dict[str, Any]] = []
    missing_grade1 = 0
    dropped: Counter[str] = Counter()
    bodies_by_topic: dict[str, set[str]] = {}
    phrase_rows: list[tuple[str, str]] = []
    for scenario in topics:
        truth = SearchTopicTruth.model_validate(scenario.truth)
        bodies_by_topic[scenario.scenario_id] = set(truth.relevance_by_body)
        if not any(grade == 2 for grade in truth.relevance_by_body.values()):
            raise SearchSetError(f"{scenario.scenario_id}: no grade-2 body")
        grade1 = [b for b, g in truth.relevance_by_body.items() if g == 1]
        if not grade1:
            missing_grade1 += 1
        for body_id, grade in truth.relevance_by_body.items():
            if body_id not in body_text:
                raise SearchSetError(f"{scenario.scenario_id}: body text missing for {body_id}")
            kind = truth.artifact_by_body.get(body_id)
            if kind not in ("mail", "document"):
                raise SearchSetError(f"{scenario.scenario_id}: invalid artifact kind for {body_id}")
            largest_box = box_by_size["5000"]
            largest_map = (
                largest_box.message_by_body if kind == "mail" else largest_box.file_by_body
            )
            if body_id not in largest_map:
                raise SearchSetError(
                    f"{scenario.scenario_id}: {body_id} kind does not match 5000 mailbox mapping"
                )
            for size, box in box_by_size.items():
                account_id = next(key for key, candidate in boxes.items() if candidate is box)
                mapping = box.message_by_body if kind == "mail" else box.file_by_body
                if body_id not in mapping:
                    if grade >= 1:
                        raise SearchSetError(
                            f"{scenario.scenario_id}: {body_id} missing from {size} mailbox"
                        )
                    continue
                artifact_id = mapping[body_id]
                account = tw.accounts[account_id]
                collection = account.messages if kind == "mail" else account.files
                item = collection.get(artifact_id)
                if item is None:
                    raise SearchSetError(
                        f"{scenario.scenario_id}: invalid {size} mapping for {body_id}"
                    )
                text = body_text.get(body_id, {}).get(account_id)
                if text is None:
                    raise SearchSetError(
                        f"{scenario.scenario_id}: {body_id} text missing from {size} world"
                    )
                if grade == 2 and not any(
                    normalize(f) in normalize(text) for f in truth.fact.surface_forms
                ):
                    raise SearchSetError(
                        f"{scenario.scenario_id}: grade-2 body {body_id} lacks fact text"
                    )
        phrase_rows.append((scenario.scenario_id, normalize(truth.topic_phrase)))
    if missing_grade1:
        raise SearchSetError(f"{missing_grade1} search topics have no grade-1 body")
    d2_bodies: set[str] = set()
    d2_path = dataset_dir / "tasks.jsonl"
    if d2_path.exists():
        for line in d2_path.read_text(encoding="utf-8").splitlines():
            task = json.loads(line)
            account_sk = sk.accounts.get(str(task.get("account_id", "")))
            if account_sk is None:
                continue
            messages = {m.id: m.body_id for m in account_sk.messages}
            files = {f.id: f.body_id for f in account_sk.files}
            for tag in task.get("tags", []):
                if tag.startswith("ref_message:"):
                    d2_bodies.update(
                        [messages[tag.removeprefix("ref_message:")]]
                        if tag.removeprefix("ref_message:") in messages
                        else []
                    )
                elif tag.startswith("ref_file:"):
                    d2_bodies.update(
                        [files[tag.removeprefix("ref_file:")]]
                        if tag.removeprefix("ref_file:") in files
                        else []
                    )
    search_bodies = set().union(*bodies_by_topic.values())
    d2_overlap = sorted(search_bodies.intersection(d2_bodies))
    # Drop every candidate in a colliding phrase group, so selection order cannot decide ownership.
    collisions: set[str] = set()
    for i, (sid, phrase) in enumerate(phrase_rows):
        for other_sid, other in phrase_rows[i + 1 :]:
            if phrase and (phrase in other or other in phrase):
                collisions.update((sid, other_sid))
    parent = {sid: sid for sid, _ in phrase_rows}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i, (sid, bodies) in enumerate(sorted(bodies_by_topic.items())):
        for other_sid, other_bodies in sorted(bodies_by_topic.items())[i + 1 :]:
            if bodies.intersection(other_bodies):
                union(sid, other_sid)
    clusters = {
        sid: "search-" + hashlib.sha256(find(sid).encode()).hexdigest()[:12]
        for sid, _ in phrase_rows
    }
    for scenario in topics:
        truth = SearchTopicTruth.model_validate(scenario.truth)
        if scenario.scenario_id in collisions:
            dropped["ambiguous_topic_phrase"] += len(FORMS)
            continue
        for form in FORMS:
            rng = random.Random(f"{seed}:{scenario.scenario_id}:{form}")  # noqa: S311
            source = max(truth.relevance_by_body, key=lambda bid: truth.relevance_by_body[bid])
            kind = truth.artifact_by_body[source]
            sizebox = box_by_size["5000"]
            source_id = (sizebox.message_by_body if kind == "mail" else sizebox.file_by_body)[
                source
            ]
            source_account = tw.accounts[next(k for k, b in boxes.items() if b is sizebox)]
            source_item = (source_account.messages if kind == "mail" else source_account.files)[
                source_id
            ]
            author = ""
            if kind == "mail":
                sender = source_item.get("from", {})
                author = str(sender.get("name", "")) if isinstance(sender, dict) else ""
            when_raw = source_item.get("sent_at", source_item.get("modified_at"))
            when = dt.datetime.fromisoformat(str(when_raw).replace("Z", "+00:00")).astimezone(
                source_account.tz
            )
            if not author:
                author = str((source_item.get("owners") or [{"name": ""}])[0].get("name", ""))
            query = search_templates.render(
                truth.topic_phrase, form, author or "담당자", when, kind, truth.fact.kind, rng
            )
            if any(normalize(surface) in normalize(query) for surface in truth.fact.surface_forms):
                dropped["answer_fact_leakage"] += 1
                continue
            if (
                sum(
                    1
                    for s in topics
                    if normalize(SearchTopicTruth.model_validate(s.truth).topic_phrase)
                    in normalize(query)
                )
                != 1
            ):
                dropped["nonunique_topic_phrase"] += 1
                continue
            relevance: dict[str, dict[str, Any]] = {}
            for size, box in box_by_size.items():
                account_id = next(k for k, b in boxes.items() if b is box)
                refs: dict[str, int] = {}
                for body_id, grade in truth.relevance_by_body.items():
                    artifact_kind = truth.artifact_by_body[body_id]
                    mapping = box.message_by_body if artifact_kind == "mail" else box.file_by_body
                    if body_id in mapping:
                        refs[mapping[body_id]] = grade
                relevance[size] = {
                    "account_id": account_id,
                    "relevance": dict(sorted(refs.items())),
                }
            candidates.append(
                {
                    "query_id": f"{scenario.scenario_id}-{form}",
                    "query": query,
                    "scenario_id": scenario.scenario_id,
                    "split": "",
                    "cluster_id": clusters[scenario.scenario_id],
                    "as_of": scenario.as_of.isoformat(),
                    "form": form,
                    "mailboxes": relevance,
                    "body_refs": dict(sorted(truth.relevance_by_body.items())),
                }
            )
    query_keys: set[str] = set()
    deduplicated: list[dict[str, Any]] = []
    for row in candidates:
        key = normalize(row["query"])
        if key in query_keys:
            dropped["duplicate_query"] += 1
            continue
        query_keys.add(key)
        deduplicated.append(row)
    candidates = deduplicated
    retained_by_topic = Counter(row["scenario_id"] for row in candidates)
    insufficient = {scenario_id for scenario_id, count in retained_by_topic.items() if count < 2}
    if insufficient:
        dropped["insufficient_forms"] += sum(
            retained_by_topic[scenario_id] for scenario_id in insufficient
        )
        candidates = [row for row in candidates if row["scenario_id"] not in insufficient]
    if not 150 <= len(candidates) <= 200:
        raise SearchSetError(f"generated query count outside 150..200: {len(candidates)}")
    assign_splits(candidates, seed)
    counts = Counter((r["split"], r["form"]) for r in candidates)
    test_clusters = len({r["cluster_id"] for r in candidates if r["split"] == "test"})
    rough_half_width = 1.96 * 0.5 / math.sqrt(test_clusters) * 100
    manifest = {
        "schema_version": "search-v1",
        "seed": seed,
        "generator_git_sha": git_sha or current_git_sha(),
        "world_sha256": tw.world_sha256,
        "skeleton_sha256": tw.skeleton_sha256,
        "query_count": len(candidates),
        "scenario_count": len(topics),
        "cluster_count": len({r["cluster_id"] for r in candidates}),
        "clusters_by_split": {
            split: len({r["cluster_id"] for r in candidates if r["split"] == split})
            for split in ("dev", "test")
        },
        "counts_by_split_form": {f"{s}:{f}": n for (s, f), n in sorted(counts.items())},
        "dropped": dict(sorted(dropped.items())),
        "missing_grade1_topics": missing_grade1,
        "d2_evidence_body_overlap_count": len(d2_overlap),
        "d2_evidence_body_overlap": d2_overlap,
        "notes": [
            "Test answers are public in this repository; sealing requires execution confirmation.",
            (
                f"Test has {test_clusters} independent topic clusters; "
                "three wording variants per topic are not independent. "
                "Rough worst-case 95% proportion half-width is about "
                f"{rough_half_width:.1f} percentage points, so retrieval confidence is limited."
            ),
            "No executor or embedding calls are included.",
        ],
        "artifacts": ["search.jsonl", "search-manifest.json"],
    }
    return candidates, manifest


def assign_splits(rows: list[dict[str, Any]], seed: int) -> None:
    clusters: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        clusters.setdefault(row["cluster_id"], []).append(row)
    ids = sorted(clusters)
    rng = random.Random(f"{seed}:search-split")  # noqa: S311
    rng.shuffle(ids)
    target = len(rows) * 7 / 12
    test: set[str] = set()
    count = 0
    for cid in ids:
        size = len(clusters[cid])
        if abs(count + size - target) < abs(count - target):
            test.add(cid)
            count += size
    tolerance = max(3.0, len(rows) * 0.05)
    if not rows or not test or len(test) == len(ids) or abs(count - target) > tolerance:
        raise SearchSetError("cannot create usable dev/test split at the requested ratio")
    for row in rows:
        row["split"] = "test" if row["cluster_id"] in test else "dev"


def load_search_set(path: Path, manifest_path: Path | None = None) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    ids: set[str] = set()
    splits: dict[str, str] = {}
    queries: set[str] = set()
    parents = list(range(len(rows)))
    scenario_owner: dict[str, int] = {}
    body_owner: dict[str, int] = {}

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parents[max(a, b)] = min(a, b)

    for row in rows:
        if not isinstance(row, dict):
            raise SearchSetError("search row must be an object")
        required = {
            "query_id",
            "query",
            "scenario_id",
            "split",
            "cluster_id",
            "as_of",
            "form",
            "mailboxes",
            "body_refs",
        }
        if not required <= row.keys():
            raise SearchSetError("search row is missing required fields")
        if not all(
            isinstance(row.get(k), str) and row[k]
            for k in ("query_id", "query", "scenario_id", "cluster_id")
        ):
            raise SearchSetError("invalid search row identifier or query")
        if row.get("split") not in ("dev", "test") or row.get("form") not in FORMS:
            raise SearchSetError("invalid split or form")
        try:
            as_of = dt.datetime.fromisoformat(str(row["as_of"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise SearchSetError("invalid as_of") from exc
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise SearchSetError("as_of must be timezone-aware")
        if row["query_id"] in ids:
            raise SearchSetError("duplicate query_id")
        ids.add(row["query_id"])
        query = normalize(row["query"])
        if not query or query in queries:
            raise SearchSetError("duplicate or empty normalized query")
        queries.add(query)
        cluster, split = row["cluster_id"], row["split"]
        if cluster in splits and splits[cluster] != split:
            raise SearchSetError("cluster spans multiple splits")
        splits[cluster] = split
        if not isinstance(row["mailboxes"], dict):
            raise SearchSetError("mailboxes must be an object")
        if set(row["mailboxes"]) != set(SIZES):
            raise SearchSetError("query must contain all mailbox sizes")
        if not isinstance(row["body_refs"], dict) or any(
            not isinstance(body_id, str)
            or not isinstance(grade, int)
            or isinstance(grade, bool)
            or grade not in (0, 1, 2)
            for body_id, grade in row["body_refs"].items()
        ):
            raise SearchSetError("invalid body_refs")
        if any(
            not isinstance(mailbox, dict)
            or not isinstance(mailbox.get("account_id"), str)
            or not isinstance(mailbox.get("relevance"), dict)
            for mailbox in row["mailboxes"].values()
        ):
            raise SearchSetError("invalid mailbox qrel structure")
        if any(
            not isinstance(grade, int) or isinstance(grade, bool) or grade not in (0, 1, 2)
            for mailbox in row["mailboxes"].values()
            for grade in mailbox["relevance"].values()
        ):
            raise SearchSetError("invalid qrel grade")
        index = len(ids) - 1
        for owner_map, key in (
            (scenario_owner, row["scenario_id"]),
            *((body_owner, body_id) for body_id in row["body_refs"]),
        ):
            previous = owner_map.get(key)
            if previous is None:
                owner_map[key] = index
            else:
                union(index, previous)
    if not 150 <= len(rows) <= 200:
        raise SearchSetError("saved search set must contain 150..200 queries")
    components: dict[int, list[int]] = {}
    for index in range(len(rows)):
        components.setdefault(find(index), []).append(index)
    for component in components.values():
        if len({rows[i]["cluster_id"] for i in component}) != 1:
            raise SearchSetError("shared scenario/body spans multiple cluster labels")
        if len({rows[i]["split"] for i in component}) != 1:
            raise SearchSetError("shared scenario/body spans multiple splits")
    if manifest_path is not None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise SearchSetError("manifest must be an object")
        if manifest.get("query_count") != len(rows):
            raise SearchSetError("manifest query_count mismatch")
        counts = Counter((row["split"], row["form"]) for row in rows)
        expected = {
            f"{split}:{form}": counts[(split, form)]
            for split in ("dev", "test")
            for form in FORMS
            if counts[(split, form)]
        }
        if manifest.get("counts_by_split_form") != expected:
            raise SearchSetError("manifest counts_by_split_form mismatch")
        cluster_counts = {
            split: len({row["cluster_id"] for row in rows if row["split"] == split})
            for split in ("dev", "test")
        }
        if manifest.get("cluster_count") != len({row["cluster_id"] for row in rows}):
            raise SearchSetError("manifest cluster_count mismatch")
        if manifest.get("clusters_by_split") != cluster_counts:
            raise SearchSetError("manifest clusters_by_split mismatch")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--git-sha")
    args = parser.parse_args()
    rows, manifest = generate(args.dataset_dir, args.seed, args.git_sha)
    out = args.out or args.dataset_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "search.jsonl").write_text(
        "".join(_format(row) + "\n" for row in rows), encoding="utf-8"
    )
    (out / "search-manifest.json").write_text(_format(manifest) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
