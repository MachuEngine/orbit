"""Deterministic retrieval query and qrel dataset generation (ADR-0026 §6)."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import random
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from orbit.evaldata.generate import git_sha as current_git_sha
from orbit.evaldata.v1 import search_templates
from orbit.evaldata.v1.body_checks import topic_body_defect
from orbit.evaldata.v1.facts import norm as normalize
from orbit.evaldata.v1.skeleton import SearchTopicTruth
from orbit.evaldata.v1.task_world import TaskWorldError, load_task_world
from orbit.evaldata.v1.templates import SEARCH_NOUNS

FORMS = ("keyword", "paraphrase_question", "person_time")
SIZES = ("200", "1000", "5000")
FROZEN_CONTRACT_SHA256 = "a5c503aa720557e7dd4e595dd5c55d0584b162539020088ff649e8da05dcd801"
BASELINE_SPLIT_SEED = 20261116
MIN_QUERIES = 150
MAX_QUERIES = 200


def _tokens(text: str) -> list[str]:
    """Split NFC/casefolded text on non-word characters, retaining Korean word boundaries."""
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFC", text).casefold(), re.UNICODE)


def _overlap(query: str, phrase: str) -> float:
    query_tokens = _tokens(query)
    topic_tokens = set(_tokens(phrase))
    return (
        sum(token in topic_tokens for token in query_tokens) / len(query_tokens)
        if query_tokens
        else 0.0
    )


def _lexical_overlap_by_form(
    rows: list[dict[str, Any]], contexts: dict[str, dict[str, Any]]
) -> dict[str, dict[str, float | int]]:
    values: dict[str, list[float]] = {form: [] for form in FORMS}
    for row in rows:
        truth = contexts[row["scenario_id"]]["truth"]
        values[row["form"]].append(_overlap(row["query"], truth.topic_phrase))
    return {
        form: {
            "count": len(scores),
            "mean": round(sum(scores) / len(scores), 8) if scores else 0.0,
            "max": round(max(scores), 8) if scores else 0.0,
        }
        for form, scores in sorted(values.items())
    }


def _outside_project(query: str, project: str) -> str:
    return normalize(query).replace(normalize(project), "")


def _assert_no_topic_noun(query: str, project: str, noun: str, phrase: str) -> None:
    normalized = normalize(query)
    if normalize(phrase) in normalized:
        raise SearchSetError("paraphrase form contains original topic phrase")
    outside = _outside_project(query, project)
    noun_tokens = [normalize(token) for token in noun.split()]
    if any(token and token in outside for token in noun_tokens):
        raise SearchSetError("paraphrase form contains original topic noun token")


def _unique_time_precision(context: dict[str, Any], contexts: list[dict[str, Any]]) -> str | None:
    """Find the coarsest source-time precision unique within project/person/fact-kind contexts."""
    when = context["when"]
    format_by_precision = {
        "month": "%Y-%m",
        "date": "%Y-%m-%d",
        "minute": "%Y-%m-%dT%H:%M",
        "second": "%Y-%m-%dT%H:%M:%S",
    }
    for precision, fmt in format_by_precision.items():
        key = when.strftime(fmt)
        matches = sum(
            other["project"] == context["project"]
            and other["author"] == context["author"]
            and other["truth"].fact.kind == context["truth"].fact.kind
            and other["when"].strftime(fmt) == key
            for other in contexts
        )
        if matches == 1:
            return precision
    return None


_TIME_FORMATS = {
    "month": "%Y-%m",
    "date": "%Y-%m-%d",
    "minute": "%Y-%m-%dT%H:%M",
    "second": "%Y-%m-%dT%H:%M:%S",
}


def _grade1_conflicts(context: dict[str, Any], precision: str) -> list[dict[str, Any]]:
    """Grade-1 bodies of the same topic that the person/period query condition also matches.

    Compared per mailbox with the project, author and the period at `precision` (account-local
    time). The artifact kind is deliberately ignored: a mail and a document of the same person
    and period are not told apart by the query conditions. The fact kind is the topic's own.
    """
    fmt = _TIME_FORMATS[precision]
    key = context["when"].strftime(fmt)
    return [
        other
        for others in context["grade1_by_size"].values()
        for other in others
        if other["project"] == context["project"]
        and other["author"] == context["author"]
        and other["when"].strftime(fmt) == key
    ]


def _choose_person_time(
    context: dict[str, Any], contexts: list[dict[str, Any]]
) -> tuple[str, bool] | None:
    """Return (precision, latest) whose condition matches no grade-1 body, or None if impossible.

    Order: the precision unique among topics, then the date, then the date plus a "latest" cue
    that holds only if every matching grade-1 body is strictly older than the grade-2 source.
    """
    selected = _unique_time_precision(context, contexts)
    if selected is None:
        return None
    candidates = [selected, "date"] if selected == "month" else [selected]
    for precision in candidates:
        if not _grade1_conflicts(context, precision):
            return precision, False
    precision = candidates[-1]
    if all(other["when"] < context["when"] for other in _grade1_conflicts(context, precision)):
        return precision, True
    return None


def _author_and_when(kind: str, item: Any, tz: dt.tzinfo) -> tuple[str, dt.datetime | None]:
    """Sender/owner name and account-local send/modify time of a mail or document."""
    if kind == "mail":
        sender = item.get("from", {})
        author = str(sender.get("name", "")) if isinstance(sender, dict) else ""
        when_raw = item.get("sent_at")
    else:
        author = str((item.get("owners") or [{"name": ""}])[0].get("name", ""))
        when_raw = item.get("modified_at")
    if not when_raw:
        return author, None
    when = dt.datetime.fromisoformat(str(when_raw).replace("Z", "+00:00")).astimezone(tz)
    return author, when


def _alias_identifies_topic(
    noun: str, alias: str, context: dict[str, Any], contexts: list[dict[str, Any]]
) -> bool:
    normalized_alias = normalize(alias)
    for other in contexts:
        other_noun = other["noun"]
        if (
            other_noun == noun
            or other["project"] != context["project"]
            or other["truth"].fact.kind != context["truth"].fact.kind
        ):
            continue
        for other_alias in search_templates.NOUN_SYNONYMS[other_noun]:
            normalized_other = normalize(other_alias)
            if normalized_alias in normalized_other or normalized_other in normalized_alias:
                return False
    return True


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
    # Suppress ambiguous keyword slots for colliding full phrases; paraphrases can disambiguate.
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
    if set(search_templates.NOUN_SYNONYMS) != set(SEARCH_NOUNS):
        raise SearchSetError("topic noun synonym table does not cover SEARCH_NOUNS exactly")
    for noun, aliases in search_templates.NOUN_SYNONYMS.items():
        if any(not isinstance(alias, str) or not normalize(alias) for alias in aliases):
            raise SearchSetError(f"topic noun {noun!r} has an empty or invalid synonym")
        distinct_aliases = {
            normalize(alias) for alias in aliases if isinstance(alias, str) and alias
        }
        if len(distinct_aliases) < 2:
            raise SearchSetError(f"topic noun {noun!r} needs at least two synonyms")
        original_tokens = [normalize(token) for token in noun.split()]
        if any(
            token and token in normalize(alias) for alias in aliases for token in original_tokens
        ):
            raise SearchSetError(f"synonym for {noun!r} repeats an original noun token")

    contexts: dict[str, dict[str, Any]] = {}
    for scenario in topics:
        truth = SearchTopicTruth.model_validate(scenario.truth)
        source = max(truth.relevance_by_body, key=lambda bid: truth.relevance_by_body[bid])
        kind = truth.artifact_by_body[source]
        body_spec = sk.bodies.get(source)
        project = body_spec.context.project if body_spec is not None else None
        if not project or not truth.topic_phrase.startswith(project + " "):
            raise SearchSetError(
                f"{scenario.scenario_id}: topic phrase has no exact project prefix"
            )
        noun = truth.topic_phrase[len(project) + 1 :]
        if not noun.strip() or noun not in SEARCH_NOUNS:
            raise SearchSetError(
                f"{scenario.scenario_id}: topic phrase has no meaningful noun split"
            )
        sizebox = box_by_size["5000"]
        source_id = (sizebox.message_by_body if kind == "mail" else sizebox.file_by_body)[source]
        source_account = tw.accounts[next(k for k, b in boxes.items() if b is sizebox)]
        source_item = (source_account.messages if kind == "mail" else source_account.files)[
            source_id
        ]
        author, when = _author_and_when(kind, source_item, source_account.tz)
        if not author or when is None:
            raise SearchSetError(f"{scenario.scenario_id}: source person or time is missing")
        grade1_by_size: dict[str, list[dict[str, Any]]] = {}
        for size, box in box_by_size.items():
            account_id = next(k for k, b in boxes.items() if b is box)
            account = tw.accounts[account_id]
            found: list[dict[str, Any]] = []
            for body_id, grade in sorted(truth.relevance_by_body.items()):
                if grade != 1:
                    continue
                other_kind = truth.artifact_by_body[body_id]
                mapping = box.message_by_body if other_kind == "mail" else box.file_by_body
                item = (account.messages if other_kind == "mail" else account.files)[
                    mapping[body_id]
                ]
                other_author, other_when = _author_and_when(other_kind, item, account.tz)
                other_spec = sk.bodies.get(body_id)
                found.append(
                    {
                        "body_id": body_id,
                        "kind": other_kind,
                        "author": other_author,
                        "when": other_when,
                        "project": other_spec.context.project if other_spec else None,
                    }
                )
            grade1_by_size[size] = found
        contexts[scenario.scenario_id] = {
            "scenario_id": scenario.scenario_id,
            "truth": truth,
            "project": project,
            "noun": noun,
            "kind": kind,
            "author": author,
            "when": when,
            "grade1_by_size": grade1_by_size,
        }
    # 본문 결함이 있는 주제는 제외 후보로 매니페스트에 적는다. 질의 행과 qrel 계약(분할 포함)은
    # 바꾸지 않는다. 실제 제외는 `search-review.jsonl`의 exclude 결정으로 한다.
    exclusion_candidates: dict[str, str] = {}
    world_year = sk.snapshot_time.year
    for scenario in topics:
        truth = SearchTopicTruth.model_validate(scenario.truth)
        defect = topic_body_defect(
            {b: (g, next(iter(body_text[b].values()))) for b, g in truth.relevance_by_body.items()},
            truth.fact.kind,
            truth.fact.surface_forms,
            world_year,
        )
        if defect is not None:
            exclusion_candidates[scenario.scenario_id] = defect
    person_time_choice: dict[str, tuple[str, bool]] = {}
    topic_drop_counts: Counter[str] = Counter()
    split_slots = [
        {"query_id": f"{scenario.scenario_id}-{form}", "cluster_id": clusters[scenario.scenario_id]}
        for scenario in topics
        for form in FORMS
    ]
    assign_splits(split_slots, BASELINE_SPLIT_SEED)
    split_by_id = {row["query_id"]: row["split"] for row in split_slots}
    for scenario in topics:
        context = contexts[scenario.scenario_id]
        truth = context["truth"]
        for form in FORMS:
            rng = random.Random(f"{seed}:{scenario.scenario_id}:{form}")  # noqa: S311
            project, noun = context["project"], context["noun"]
            author, when, kind = context["author"], context["when"], context["kind"]
            synonym: str | None = None
            precision = "month"
            latest = False
            if form == "keyword" and scenario.scenario_id in collisions:
                dropped["ambiguous_topic_phrase"] += 1
                topic_drop_counts[scenario.scenario_id] += 1
                continue
            if form == "paraphrase_question":
                alias_candidates = list(search_templates.NOUN_SYNONYMS[noun])
                rng.shuffle(alias_candidates)
                for alias in alias_candidates:
                    # Only competing topics in this project and fact kind make an alias ambiguous.
                    if _alias_identifies_topic(noun, alias, context, list(contexts.values())):
                        synonym = alias
                        break
                if synonym is None:
                    dropped["ambiguous_synonym"] += 1
                    topic_drop_counts[scenario.scenario_id] += 1
                    continue
            if form == "person_time":
                choice = _choose_person_time(context, list(contexts.values()))
                if choice is None:
                    reason = (
                        "ambiguous_person_time"
                        if _unique_time_precision(context, list(contexts.values())) is None
                        else "person_time_matches_grade1"
                    )
                    dropped[reason] += 1
                    topic_drop_counts[scenario.scenario_id] += 1
                    continue
                precision, latest = choice
                person_time_choice[f"{scenario.scenario_id}-{form}"] = choice
            query = search_templates.render(
                truth.topic_phrase,
                form,
                author,
                when,
                kind,
                truth.fact.kind,
                rng,
                project=project,
                noun=noun,
                synonym=synonym,
                time_precision=precision,
                latest=latest,
            )
            if any(normalize(surface) in normalize(query) for surface in truth.fact.surface_forms):
                raise SearchSetError(
                    f"{scenario.scenario_id}: answer fact leaked into {form} query"
                )
            missing_field = search_templates.missing_required_field(
                query,
                form,
                project,
                truth.topic_phrase,
                truth.fact.kind,
                synonym=synonym,
                author=author,
                when=when,
                time_precision=precision,
                latest=latest,
            )
            if missing_field is not None:
                raise SearchSetError(
                    f"{scenario.scenario_id}: {form} query is missing required {missing_field}"
                )
            if form != "keyword":
                _assert_no_topic_noun(query, project, noun, truth.topic_phrase)
                if form == "paraphrase_question" and synonym is None:
                    raise SearchSetError("question query has no topic synonym")
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
                    "split": split_by_id[f"{scenario.scenario_id}-{form}"],
                    "cluster_id": clusters[scenario.scenario_id],
                    "as_of": scenario.as_of.isoformat(),
                    "form": form,
                    "mailboxes": relevance,
                    "body_refs": dict(sorted(truth.relevance_by_body.items())),
                }
            )
    candidates = _deduplicate_queries(candidates, dropped, topic_drop_counts)
    if over_dropped := {sid: count for sid, count in topic_drop_counts.items() if count > 1}:
        raise SearchSetError(f"more than one query dropped for a topic: {over_dropped}")
    if not MIN_QUERIES <= len(candidates) <= MAX_QUERIES:
        raise SearchSetError(
            f"generated query count outside {MIN_QUERIES}..{MAX_QUERIES}: {len(candidates)}"
        )
    precision_counts = Counter(
        person_time_choice[r["query_id"]][0] for r in candidates if r["form"] == "person_time"
    )
    latest_count = sum(
        person_time_choice[r["query_id"]][1] for r in candidates if r["form"] == "person_time"
    )
    counts = Counter((r["split"], r["form"]) for r in candidates)
    test_clusters = len({r["cluster_id"] for r in candidates if r["split"] == "test"})
    rough_half_width = 1.96 * 0.5 / math.sqrt(test_clusters) * 100
    manifest = {
        "schema_version": "search-v1",
        "seed": seed,
        "split_seed": BASELINE_SPLIT_SEED,
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
        "dropped_by_topic": dict(sorted(topic_drop_counts.items())),
        "exclusion_candidates": dict(sorted(exclusion_candidates.items())),
        "exclusion_candidate_count": len(exclusion_candidates),
        "exclusion_candidates_by_reason": dict(
            sorted(Counter(exclusion_candidates.values()).items())
        ),
        "synonym_noun_count": len(search_templates.NOUN_SYNONYMS),
        "synonym_variant_count": sum(
            len({normalize(alias) for alias in aliases})
            for aliases in search_templates.NOUN_SYNONYMS.values()
        ),
        "person_time_precision_counts": dict(sorted(precision_counts.items())),
        "person_time_latest_count": latest_count,
        "lexical_overlap_by_form": _lexical_overlap_by_form(candidates, contexts),
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


def _deduplicate_queries(
    rows: list[dict[str, Any]], dropped: Counter[str], per_topic: Counter[str]
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = normalize(row["query"])
        if key in seen:
            dropped["duplicate_query"] += 1
            per_topic[row["scenario_id"]] += 1
            continue
        seen.add(key)
        unique.append(row)
    return unique


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


SEARCH_REVIEW_FILE = "search-review.jsonl"


def read_search_review(directory: Path) -> dict[str, dict[str, str]]:
    """`search-review.jsonl`(질의 단위 검수 결정)을 읽는다. 같은 query_id는 마지막 줄이 이긴다.

    각 값은 `{"decision": "approve|fix|exclude", "note": "..."}`다. 파일이 없으면 빈 dict다.
    """
    file = directory / SEARCH_REVIEW_FILE
    if not file.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("query_id"), str)
            or row.get("decision") not in ("approve", "fix", "exclude")
        ):
            raise SearchSetError("invalid search-review row")
        out[row["query_id"]] = {"decision": row["decision"], "note": str(row.get("note", ""))}
    return out


def load_search_set(
    path: Path, manifest_path: Path | None = None, *, include_excluded: bool = False
) -> list[dict[str, Any]]:
    """검색 세트를 읽고 검증한다. 같은 폴더의 `search-review.jsonl`에서 제외된 질의는 기본으로 뺀다.

    검증(개수·분할·매니페스트 일치)은 제외 전 전체 행에 건다.
    """
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
    if not MIN_QUERIES <= len(rows) <= MAX_QUERIES:
        raise SearchSetError(f"saved search set must contain {MIN_QUERIES}..{MAX_QUERIES} queries")
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
        if manifest.get("split_seed") != BASELINE_SPLIT_SEED:
            raise SearchSetError("manifest split_seed mismatch")
        if manifest.get("synonym_noun_count", 0) < len(SEARCH_NOUNS) or manifest.get(
            "synonym_variant_count", 0
        ) < 2 * len(SEARCH_NOUNS):
            raise SearchSetError("manifest synonym metadata is incomplete")
        if not isinstance(manifest.get("dropped_by_topic"), dict) or any(
            not isinstance(count, int) or count < 0 or count > 1
            for count in manifest["dropped_by_topic"].values()
        ):
            raise SearchSetError("manifest dropped_by_topic is invalid")
        overlap = manifest.get("lexical_overlap_by_form")
        if not isinstance(overlap, dict) or set(overlap) != set(FORMS):
            raise SearchSetError("manifest lexical_overlap_by_form is invalid")
        for form, stats in overlap.items():
            if (
                not isinstance(stats, dict)
                or stats.get("count") != counts[("dev", form)] + counts[("test", form)]
                or not isinstance(stats.get("mean"), (int, float))
                or not isinstance(stats.get("max"), (int, float))
                or not 0 <= stats["mean"] <= stats["max"] <= 1
            ):
                raise SearchSetError("manifest lexical overlap statistics mismatch")
    if not include_excluded:
        review = read_search_review(path.parent)
        rows = [r for r in rows if review.get(r["query_id"], {}).get("decision") != "exclude"]
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
