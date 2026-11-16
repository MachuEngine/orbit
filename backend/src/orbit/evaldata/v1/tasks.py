"""종단 과제·스모크 생성기 (ADR-0026 §4·§5).

```bash
cd backend
uv run python -m orbit.evaldata.v1.tasks --dataset-dir ../eval/datasets/orbit-eval/v1 \\
    --seed 20261116 [--skip-paraphrase | --max-cost-usd X [--dry-run]] [--out <dir>]
```

입력은 같은 디렉터리의 `skeleton.json`(시나리오 기록)과 `world.json`(세계)이다.
정답은 코드가 세계에서
다시 계산한다(`task_gold`). 출력은 `tasks.jsonl`과 `manifest.json`이고, 표현 변형 결과는
`paraphrase.jsonl`에 쌓는다. 같은 입력·시드·git SHA·저장된 표현 변형이면
출력은 바이트 단위로 같다.

생성 순서
1. 시나리오마다 가능한 형태(단일 턴, 다중 턴, 리마인더 요청·마감 질문)의 설계도를 만든다.
2. 유형별 쿼터대로 사용자·계열·갈래가 고르게 원본을 고른다. 근거 아티팩트를 공유하거나 정답 지문이
   같은 설계도는 건너뛴다. 쓰기 일부는 승인 거절 과제(`approval_policy=["reject"]`)로 만든다.
3. 원본 중에서 장애 주입 파생(`-fault`)과 표현 변형 파생(`-para`)을 만든다. 같은 시나리오·묶음이다.
4. 근거와 시나리오를 공유하는 과제를 묶음으로 묶고 묶음 단위로 dev/test를 나눈다.
5. dev에서 스모크 25문항을 고른다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import shutil
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from orbit.evaldata import dataset as ds
from orbit.evaldata.generate import git_sha
from orbit.evaldata.schema import Counts, Manifest, SplitRatio, Task, Turn
from orbit.evaldata.v1 import paraphrase as pp
from orbit.evaldata.v1.body_checks import foreign_years
from orbit.evaldata.v1.realize_models import (
    FamilyModel,
    LLMFactory,
    ModelConfigError,
    load_models,
)
from orbit.evaldata.v1.task_gold import (
    BUILDERS,
    Ctx,
    Draft,
    TaskGenError,
    forms_of,
    ref_tags,
)
from orbit.evaldata.v1.task_world import TaskWorld, TaskWorldError, load_task_world

SUITE_VERSION = "tasks-v1"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_COST_CAP = 3
EXIT_FATAL = 4
EXIT_PLAN = 5  # 쿼터·분할 목표를 채우지 못했다

DERIVED_GROUPS = ("fault", "paraphrase")
READ_FAULT_ERRORS = ("transient", "rate_limited")


class SuiteError(RuntimeError):
    """쿼터·분할 목표를 채우지 못했다. 메시지에 이유가 있다."""


# ---------------------------------------------------------------- 계획


@dataclass(frozen=True)
class Slot:
    """원본 선택 쿼터 하나. `variant`가 있으면 그 갈래의 설계도만 쓴다."""

    scenario_type: str
    form: str
    n: int
    variant: str | None = None


@dataclass(frozen=True)
class SuitePlan:
    slots: tuple[Slot, ...]
    reject_slots: tuple[tuple[str, str | None], ...]  # (시나리오 유형, 갈래)마다 1건을 거절 과제로
    fault_reads: dict[str, int]  # 유형 이름 -> 읽기 장애 파생 수
    fault_writes: int
    paraphrase_need: int
    paraphrase_weights: dict[str, int]
    paraphrase_oversample: float = 1.6
    test_share: float = 7 / 12  # test 140 / 전체 240
    test_tolerance: int = 4  # 목표 test 문항 수와의 허용 차이
    min_test_clusters: int = 100
    smoke_size: int = 25


DEFAULT_PLAN = SuitePlan(
    slots=(
        Slot("day_agenda", "single", 4),
        Slot("free_slot", "single", 4),
        Slot("reschedule", "single", 4),
        Slot("cancellation", "single", 4),
        Slot("project_week", "single", 4),
        Slot("person_next", "single", 4),
        Slot("cross_midnight", "single", 2),
        Slot("tz_event", "single", 4),
        Slot("all_day", "single", 2),
        Slot("mail_fact", "single", 40),
        Slot("meeting_request", "single", 6),
        Slot("event_change_target", "single", 4, "move"),
        Slot("event_change_target", "single", 4, "cancel"),
        Slot("reply_needed", "single", 6),
        Slot("person_next", "mt", 4),
        Slot("day_agenda", "mt", 4),
        Slot("mail_fact", "mt", 12),
        Slot("doc_fact", "single", 8),
        Slot("doc_version", "single", 8),
        Slot("deadline", "request", 12),
        Slot("deadline", "ask", 4),
        Slot("mismatch", "single", 16),
        Slot("absent", "single", 4, "person_no_meeting"),
        Slot("absent", "single", 4, "no_document"),
        Slot("absent", "single", 4, "no_amount"),
        Slot("absent", "single", 4, "project_no_meeting"),
        Slot("ambiguous", "single", 6, "person_meetings"),
        Slot("ambiguous", "single", 5, "same_project_notes"),
        Slot("ambiguous", "single", 5, "homonym_person"),
    ),
    reject_slots=(
        ("meeting_request", None),
        ("event_change_target", "move"),
        ("event_change_target", "cancel"),
        ("reply_needed", None),
    ),
    fault_reads={"calendar": 8, "mail": 6, "mismatch": 2},
    fault_writes=8,
    paraphrase_need=24,
    paraphrase_weights={"calendar": 10, "mail": 8, "mismatch": 3, "document": 3},
)
PARAPHRASE_KINDS = ("calendar", "mail", "mismatch", "document")


# ---------------------------------------------------------------- 설계도 만들기


@dataclass
class Candidates:
    drafts: dict[tuple[str, str], Draft] = field(default_factory=dict)  # (scenario_id, form)
    dropped: Counter[str] = field(default_factory=Counter)  # "유형/형태: 사유"


FOREIGN_YEAR_REASON = "근거 본문에 세계 기준 연도가 아닌 연도 표기가 있다"


def _evidence_foreign_years(account: Any, evidence: Sequence[Any], world_year: int) -> list[str]:
    """근거 메일·문서 본문에 `world_year`가 아닌 연도 표기가 있으면 그 연도들."""
    found: list[str] = []
    for ref in evidence:
        if ref.kind == "message":
            text = str(account.message_of(ref).get("body_text", ""))
        elif ref.kind == "file":
            text = str(account.file_of(ref).get("text", ""))
        else:
            continue
        found += [y for y in foreign_years(text, world_year) if y not in found]
    return found


def build_candidates(ctx: Ctx) -> Candidates:
    """모든 시나리오·형태의 설계도를 만든다. 세계와 어긋나면 `TaskGenError`다."""
    out = Candidates()
    for sc in ctx.tw.skeleton.scenarios:
        builder = BUILDERS.get(sc.type)
        if builder is None:
            continue  # search_topic은 검색 세트 몫이다
        account = ctx.acct(sc)
        for ref in [*sc.evidence, *sc.distractors]:
            if ref.kind == "event":
                account.event_of(ref)
            elif ref.kind == "message":
                account.message_of(ref)
            else:
                account.file_of(ref)
        world_year = ctx.tw.skeleton.snapshot_time.year
        if _evidence_foreign_years(account, sc.evidence, world_year):
            for form in forms_of(sc.type):
                out.dropped[f"{sc.type}/{form}: {FOREIGN_YEAR_REASON}"] += 1
            continue
        for form in forms_of(sc.type):
            result = builder(ctx, sc, form)
            if isinstance(result, str):
                out.dropped[f"{sc.type}/{form}: {result}"] += 1
            else:
                result.refs = tuple(sorted(set(result.refs) | set(ref_tags(sc.evidence))))
                out.drafts[(sc.scenario_id, form)] = result
    return out


def fingerprint(d: Draft) -> str:
    """유형·채점 대상의 정규화 해시. 보조 사실과 질문 문장은 넣지 않는다.

    집합 순서와 시간대 표기만 다른 답은 같다. 기권·승인 거절은 질문 대상도 포함한다.
    장애·표현 변형은 원본을 복제하는 실험이므로 원본 선택에만 중복 제거를 적용한다.
    """
    g = d.gold
    payload = {
        "type": d.kind,
        "answer": g.expected_answer.model_dump() if g.expected_answer else None,
        "changes": [c.model_dump() for c in g.expected_changes or []],
        "facts": [f.any_of for f in g.answer_facts]
        if g.expected_answer is None or g.answer_facts_required
        else [],
        "must_not": g.must_not_contain,
        "account": d.scenario.account_id,  # 다른 메일함의 같은 값은 다른 과제다
        "extra": d.fingerprint_extra,
    }
    raw = json.dumps(_canonical(payload), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return sorted(
            (_canonical(v) for v in value),
            key=lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True),
        )
    return value


# ---------------------------------------------------------------- 고르게 고르기


@dataclass
class Taken:
    scenarios: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)
    fingerprints: set[str] = field(default_factory=set)


@dataclass
class Balance:
    """그룹(유형)별로 사용자·계열·갈래가 몇 번 뽑혔는지."""

    counts: Counter[tuple[str, str, str]] = field(default_factory=Counter)

    def key(self, group: str, d: Draft, slot_label: str) -> tuple[int, int, int]:
        return (
            self.counts[(group, "variant:" + slot_label, d.variant or d.scenario.type)],
            self.counts[(group, "user", d.scenario.account_id)],
            self.counts[(group, "family", d.scenario.generator_family)],
        )

    def add(self, group: str, d: Draft, slot_label: str) -> None:
        self.counts[(group, "variant:" + slot_label, d.variant or d.scenario.type)] += 1
        self.counts[(group, "user", d.scenario.account_id)] += 1
        self.counts[(group, "family", d.scenario.generator_family)] += 1


@dataclass
class SelectStats:
    ref_conflicts: set[str] = field(default_factory=set)
    duplicate_answers: set[str] = field(default_factory=set)


def _shuffled(pool: list[Draft], rng: random.Random) -> list[Draft]:
    ordered = sorted(pool, key=lambda d: d.scenario.scenario_id)
    rng.shuffle(ordered)
    return ordered


def pick_balanced(
    pool: list[Draft],
    n: int,
    rng: random.Random,
    balance: Balance,
    group: str,
    slot_label: str,
    taken: Taken | None,
    stats: SelectStats | None = None,
) -> list[Draft]:
    """사용자·계열·갈래 순으로 가장 적게 뽑힌 설계도를 `n`개 고른다.

    `taken`이 있으면 이미 뽑힌 시나리오·근거·정답 지문과 겹치는 설계도는 건너뛴다. 못 채우면
    `SuiteError`다.
    """
    order = _shuffled(pool, rng)
    chosen: list[Draft] = []
    while len(chosen) < n:
        best: tuple[tuple[int, int, int, int], Draft] | None = None
        for idx, d in enumerate(order):
            if taken is not None:
                if d.scenario.scenario_id in taken.scenarios:
                    continue
                if set(d.refs) & taken.refs:
                    if stats is not None:
                        stats.ref_conflicts.add(d.scenario.scenario_id)
                    continue
                if fingerprint(d) in taken.fingerprints:
                    if stats is not None:
                        stats.duplicate_answers.add(d.scenario.scenario_id)
                    continue
            key = (*balance.key(group, d, slot_label), idx)
            if best is None or key < best[0]:
                best = (key, d)
        if best is None:
            raise SuiteError(
                f"{group}/{slot_label}: 후보 {len(order)}개 중 {n}개를 채우지 못했다"
                f"(고른 {len(chosen)}개). 근거 공유·정답 지문 중복·버려진 설계도를 확인한다"
            )
        d = best[1]
        chosen.append(d)
        balance.add(group, d, slot_label)
        if taken is not None:
            taken.scenarios.add(d.scenario.scenario_id)
            taken.refs |= set(d.refs)
            taken.fingerprints.add(fingerprint(d))
        order.remove(d)
    return chosen


@dataclass
class Original:
    task_id: str
    draft: Draft
    slot: Slot


def task_id_of(d: Draft) -> str:
    base = d.scenario.scenario_id.removeprefix("sc-")
    return f"{'mt' if d.kind == 'multi_turn' else 't'}-{base}"


def select_originals(
    cands: Candidates, plan: SuitePlan, seed: int
) -> tuple[list[Original], SelectStats]:
    """쿼터 순서(후보가 적은 슬롯부터)대로 원본을 고른다. 쓰기 일부는 거절 과제로 바꾼다."""
    taken, balance, stats = Taken(), Balance(), SelectStats()
    pools: dict[Slot, list[Draft]] = {}
    for slot in plan.slots:
        pools[slot] = [
            d
            for (sid, form), d in cands.drafts.items()
            if d.scenario.type == slot.scenario_type
            and form == slot.form
            and (slot.variant is None or d.variant == slot.variant)
        ]
    chosen: dict[Slot, list[Draft]] = {}
    # 다중 턴은 단일 턴 쿼터를 먼저 채운 뒤 남은 시나리오에서 고른다
    single = [s for s in plan.slots if s.form != "mt"]
    multi = [s for s in plan.slots if s.form == "mt"]
    for batch in (single, multi):
        # 모호성 근거는 일반 조회와 자주 겹친다. 먼저 확보해 특정 사용자가 빠지지 않게 한다.
        for slot in sorted(
            batch,
            key=lambda s: (s.scenario_type != "ambiguous", len(pools[s]), plan.slots.index(s)),
        ):
            label = f"{slot.scenario_type}/{slot.form}/{slot.variant or '*'}"
            rng = random.Random(f"{seed}:select:{label}")  # noqa: S311  재현성용
            group = pools[slot][0].kind if pools[slot] else slot.scenario_type
            chosen[slot] = pick_balanced(
                pools[slot], slot.n, rng, balance, group, label, taken, stats
            )
    originals: list[Original] = []
    reject_keys = set(plan.reject_slots)
    for slot in plan.slots:
        for i, d in enumerate(chosen[slot]):
            if i == 0 and (slot.scenario_type, slot.variant) in reject_keys:
                d = to_reject(d)
            originals.append(Original(task_id_of(d), d, slot))
    originals.sort(key=lambda o: o.task_id)
    return originals, stats


def to_reject(d: Draft) -> Draft:
    """승인 요청을 거절하는 과제로 바꾼다. 거절하면 아무것도 바뀌지 않아야 한다."""
    planned = [c.model_dump(mode="json") for c in d.gold.expected_changes or []]
    gold = d.gold.model_copy(update={"expected_changes": []})
    return Draft(
        d.scenario,
        d.kind,
        d.form,
        d.turns,
        gold,
        d.refs,
        keep=d.keep,
        difficulty={**d.difficulty, "approval": "reject"},
        approval_policy=["reject"],
        fingerprint_extra={"rejected": planned},
        fault_tool=None,
        note=d.note,
        variant=d.variant,
    )


# ---------------------------------------------------------------- 파생 과제


@dataclass
class Derived:
    task_id: str
    origin: Original
    kind: str  # "fault" | "paraphrase"
    fault_plan: dict[str, Any] | None = None
    fault_error: str | None = None


def pick_faults(originals: list[Original], plan: SuitePlan, seed: int) -> list[Derived]:
    """읽기 파생(첫 조회 호출에 일시 오류·호출 제한)과 쓰기 파생(결과 불명, 적용됨)을 고른다."""
    out: list[Derived] = []
    balance = Balance()
    reads: list[Draft] = []
    by_id = {o.draft.scenario.scenario_id: o for o in originals}
    eligible = [
        o
        for o in originals
        if o.draft.fault_tool is not None
        and o.draft.form != "mt"
        and o.draft.approval_policy is None
    ]
    for kind, n in sorted(plan.fault_reads.items()):
        pool = [o.draft for o in eligible if o.draft.kind == kind and not o.draft.writes]
        rng = random.Random(f"{seed}:fault:{kind}")  # noqa: S311
        reads += pick_balanced(pool, n, rng, balance, kind, "read", None)
    for i, d in enumerate(sorted(reads, key=lambda d: d.scenario.scenario_id)):
        err = READ_FAULT_ERRORS[i % len(READ_FAULT_ERRORS)]
        out.append(_fault(by_id[d.scenario.scenario_id], err))
    write_pool = [o.draft for o in eligible if o.draft.kind == "write"]
    rng = random.Random(f"{seed}:fault:write")  # noqa: S311
    writes = pick_balanced(write_pool, plan.fault_writes, rng, balance, "write", "write", None)
    out += [_fault(by_id[d.scenario.scenario_id], "result_unknown") for d in writes]
    out.sort(key=lambda x: x.task_id)
    return out


def _fault(origin: Original, error: str) -> Derived:
    tool = origin.draft.fault_tool
    if tool is None:
        raise SuiteError("장애를 적용할 도구가 없다")
    rule = {"tool": tool, "nth_call": 1, "error": error}
    return Derived(origin.task_id + "-fault", origin, "fault", {"rules": [rule]}, error)


def paraphrase_candidates(
    originals: list[Original], faults: list[Derived], plan: SuitePlan, seed: int
) -> list[pp.Source]:
    """표현 변형 후보를 처리 순서대로 돌려준다(유형 비중대로 섞는다). 장애 파생 원본은 뒤로 "
    "미룬다."""
    faulted = {f.origin.task_id for f in faults}
    by_kind: dict[str, list[Original]] = defaultdict(list)
    for o in originals:
        if o.draft.paraphrasable and o.draft.kind in PARAPHRASE_KINDS and not o.draft.writes:
            by_kind[o.draft.kind].append(o)
    lists: dict[str, list[pp.Source]] = {}
    balance = Balance()
    for kind, weight in plan.paraphrase_weights.items():
        pool = by_kind.get(kind, [])
        rng = random.Random(f"{seed}:paraphrase:{kind}")  # noqa: S311
        fresh = [o for o in pool if o.task_id not in faulted]
        used = [o for o in pool if o.task_id in faulted]
        order: list[Draft] = []
        for part in (fresh, used):
            if part:
                order += pick_balanced(
                    [o.draft for o in part], len(part), rng, balance, kind, "para", None
                )
        want = min(len(order), math.ceil(weight * plan.paraphrase_oversample))
        by_scenario = {o.draft.scenario.scenario_id: o for o in pool}
        lists[kind] = [
            pp.Source(
                origin_id=by_scenario[d.scenario.scenario_id].task_id,
                question=d.turns[0],
                keep=d.keep,
                family=d.scenario.generator_family,
            )
            for d in order[:want]
        ]
    return _interleave(lists, plan.paraphrase_weights)


def _interleave(lists: dict[str, list[pp.Source]], weights: dict[str, int]) -> list[pp.Source]:
    """비중에 맞춰 번갈아 꺼낸다(걸음 스케줄링)."""
    out: list[pp.Source] = []
    taken = {k: 0 for k in lists}
    while any(taken[k] < len(lists[k]) for k in lists):
        k = min(
            (k for k in lists if taken[k] < len(lists[k])),
            key=lambda k: ((taken[k] + 1) / weights[k], k),
        )
        out.append(lists[k][taken[k]])
        taken[k] += 1
    return out


def accepted_paraphrases(
    candidates: Sequence[pp.Source], rows: dict[str, dict[str, Any]], need: int
) -> list[tuple[pp.Source, str]]:
    """후보 순서에서 검증을 통과한 변형문을 앞에서 `need`개까지 고른다."""
    out: list[tuple[pp.Source, str]] = []
    for src in candidates:
        row = rows.get(src.origin_id)
        if pp.usable_row(row, src) and row is not None and row["status"] == "ok":
            out.append((src, str(row["text"])))
            if len(out) == need:
                break
    return out


# ---------------------------------------------------------------- Task 만들기


def _tags(d: Draft, derived: str, extra: Sequence[str] = ()) -> list[str]:
    sc = d.scenario
    tags = [
        f"type:{d.kind}",
        f"scenario:{sc.type}",
        f"family:{sc.generator_family}",
        f"derived:{derived}",
        f"user:{sc.account_id}",
    ]
    if d.variant:
        tags.append(f"variant:{d.variant}")
    if d.approval_policy:
        tags.append("approval:reject")
    tags += extra
    return [*tags, *d.refs]


def make_task(
    tw: TaskWorld,
    task_id: str,
    d: Draft,
    *,
    turns: list[str] | None = None,
    derived: str = "none",
    origin_id: str | None = None,
    fault_plan: dict[str, Any] | None = None,
    extra_tags: Sequence[str] = (),
) -> Task:
    sc = d.scenario
    acct = tw.account(sc.account_id)
    difficulty = dict(d.difficulty)
    if origin_id is not None:
        difficulty["derived_from"] = origin_id
    return Task(
        task_id=task_id,
        scenario_id=sc.scenario_id,
        cluster_id="",
        split="dev",
        as_of_time=sc.as_of,
        account_id=sc.account_id,
        turns=[Turn(role="user", content=t) for t in (turns or d.turns)],
        gold=d.gold,
        difficulty=difficulty,
        tags=_tags(d, derived, extra_tags),
        approval_policy=list(d.approval_policy) if d.approval_policy else None,
        time_zone=acct.time_zone,
        fault_plan=fault_plan,
    )


def group_of(task: Task) -> str:
    """분할·스모크의 유형 묶음: 파생 과제는 `fault`·`paraphrase`, 아니면 유형 이름."""
    tags = set(task.tags)
    for g in DERIVED_GROUPS:
        if f"derived:{g}" in tags:
            return g
    return next(t[5:] for t in task.tags if t.startswith("type:"))


# ---------------------------------------------------------------- 묶음과 분할


def clusters_of(tasks: Sequence[Task]) -> dict[str, list[Task]]:
    """근거 아티팩트나 시나리오를 공유하는 과제를 묶는다(union-find). 키는 묶음 ID다."""
    parent = list(range(len(tasks)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[str, int] = {}
    for i, t in enumerate(tasks):
        for key in [*sorted(ds.evidence_refs(t)), f"scenario:{t.scenario_id}"]:
            if key in first:
                parent[find(i)] = find(first[key])
            else:
                first[key] = i
    groups: dict[int, list[Task]] = {}
    for i, t in enumerate(tasks):
        groups.setdefault(find(i), []).append(t)
    return {ds.cluster_id_of(g): g for g in groups.values()}


@dataclass(frozen=True)
class SplitResult:
    test_clusters: frozenset[str]
    cost: int
    attempts: int


def assign_splits(
    clusters: dict[str, list[Task]], plan: SuitePlan, seed: int, attempts: int = 40
) -> SplitResult:
    """묶음 단위로 test를 고른다. 유형 묶음별 test 문항 수가 목표(전체의 `test_share`)에 가깝게.

    무작위 시작 + 한 묶음씩 뒤집는 지역 탐색을 시드 고정으로 `attempts`번 해 가장 좋은 것을 쓴다.
    """
    ids = sorted(clusters)
    weights: dict[str, Counter[str]] = {c: Counter(group_of(t) for t in clusters[c]) for c in ids}
    totals: Counter[str] = Counter()
    for c in ids:
        totals += weights[c]
    target = {g: totals[g] * plan.test_share for g in totals}
    total_target = sum(totals.values()) * plan.test_share

    def cost_of(test: set[str]) -> float:
        got: Counter[str] = Counter()
        for c in test:
            got += weights[c]
        per_group = sum(abs(got[g] - target[g]) for g in totals)
        return per_group + 3 * abs(sum(got.values()) - total_target)

    best: tuple[float, frozenset[str]] | None = None
    for attempt in range(attempts):
        rng = random.Random(f"{seed}:split:{attempt}")  # noqa: S311
        order = ids[:]
        rng.shuffle(order)
        test: set[str] = set()
        got = 0.0
        for c in order:  # 목표에 가깝게 채우는 시작점
            w = sum(weights[c].values())
            if abs(got + w - total_target) <= abs(got - total_target):
                test.add(c)
                got += w
        improved = True
        while improved:
            improved = False
            for c in order:
                trial = test ^ {c}
                if len(trial) >= plan.min_test_clusters and cost_of(trial) < cost_of(test):
                    test, improved = trial, True
        score = cost_of(test) + (1000 if len(test) < plan.min_test_clusters else 0)
        if best is None or score < best[0]:
            best = (score, frozenset(test))
    if best is None:
        raise SuiteError("분할 탐색 시도는 1회 이상이어야 한다")
    return SplitResult(best[1], round(best[0]), attempts)


def check_split(tasks: Sequence[Task], plan: SuitePlan) -> list[str]:
    """분할 목표를 점검한다. 문제 목록을 돌려준다(비면 통과)."""
    problems: list[str] = []
    n = len(tasks)
    test = [t for t in tasks if t.split == "test"]
    target = round(n * plan.test_share)
    if abs(len(test) - target) > plan.test_tolerance:
        problems.append(
            f"test 문항 {len(test)}개가 목표 {target}개에서 {plan.test_tolerance}개 넘게 벗어났다"
        )
    clusters = {t.cluster_id for t in test}
    if len(clusters) < plan.min_test_clusters:
        problems.append(f"test 독립 묶음 {len(clusters)}개가 {plan.min_test_clusters}개보다 적다")
    by_group: dict[str, Counter[str]] = defaultdict(Counter)
    for t in tasks:
        by_group[group_of(t)][t.split] += 1
    for g, c in sorted(by_group.items()):
        if sum(c.values()) >= 2 and (c["dev"] == 0 or c["test"] == 0):
            problems.append(f"{g}: dev {c['dev']}개, test {c['test']}개로 한쪽이 비었다")
    return problems


# ---------------------------------------------------------------- 스모크


def select_smoke(tasks: Sequence[Task], size: int, seed: int) -> list[str]:
    """dev 과제에서 유형 묶음마다 최소 1개, 나머지는 문항 수 비율로 `size`개를 고른다."""
    dev = sorted((t for t in tasks if t.split == "dev"), key=lambda t: t.task_id)
    by_group: dict[str, list[Task]] = defaultdict(list)
    for t in dev:
        by_group[group_of(t)].append(t)
    groups = sorted(by_group)
    size = min(size, len(dev))
    total = sum(len(by_group[g]) for g in groups)
    quota = {g: min(1, len(by_group[g])) for g in groups}
    while sum(quota.values()) < size:  # 문항 수 비율에 가장 모자란 유형부터 하나씩 더한다
        open_groups = [g for g in groups if quota[g] < len(by_group[g])]
        if not open_groups:
            break
        g = min(open_groups, key=lambda g: (-(size * len(by_group[g]) / total - quota[g]), g))
        quota[g] += 1
    picked: list[str] = []
    for g in groups:
        rng = random.Random(f"{seed}:smoke:{g}")  # noqa: S311
        clusters: dict[str, list[Task]] = defaultdict(list)
        for t in by_group[g]:
            clusters[t.cluster_id].append(t)
        order = sorted(clusters)
        rng.shuffle(order)
        take: list[Task] = []
        while len(take) < min(quota[g], len(by_group[g])):
            for c in order:
                if clusters[c] and len(take) < quota[g]:
                    take.append(clusters[c].pop(0))
        picked += [t.task_id for t in take]
    return sorted(picked)


# ---------------------------------------------------------------- 조립


@dataclass
class Suite:
    tasks: list[Task]
    smoke_ids: list[str]
    details: dict[str, Any]
    problems: list[str]


def build_suite(
    tw: TaskWorld,
    seed: int,
    plan: SuitePlan,
    *,
    rows: dict[str, dict[str, Any]] | None = None,
    include_paraphrase: bool = True,
    candidates: Candidates | None = None,
) -> tuple[Suite, list[pp.Source]]:
    """과제 전체를 만든다. 두 번째 값은 표현 변형 후보(처리 순서)다."""
    ctx = Ctx(tw, seed)
    cands = candidates or build_candidates(ctx)
    originals, stats = select_originals(cands, plan, seed)
    faults = pick_faults(originals, plan, seed)
    sources = paraphrase_candidates(originals, faults, plan, seed)
    accepted = (
        accepted_paraphrases(sources, rows or {}, plan.paraphrase_need)
        if include_paraphrase
        else []
    )

    tasks: list[Task] = []
    by_id = {o.task_id: o for o in originals}
    for o in originals:
        tasks.append(make_task(tw, o.task_id, o.draft))
    for f in faults:
        extra = [f"fault:{f.fault_error}"]
        tasks.append(
            make_task(
                tw,
                f.task_id,
                f.origin.draft,
                derived="fault",
                origin_id=f.origin.task_id,
                fault_plan=f.fault_plan,
                extra_tags=extra,
            )
        )
    for src, text in accepted:
        o = by_id[src.origin_id]
        tasks.append(
            make_task(
                tw,
                o.task_id + "-para",
                o.draft,
                turns=[text],
                derived="paraphrase",
                origin_id=o.task_id,
            )
        )
    tasks.sort(key=lambda t: t.task_id)

    clusters = clusters_of(tasks)
    split = assign_splits(clusters, plan, seed)
    cluster_of = {t.task_id: cid for cid, g in clusters.items() for t in g}
    tasks = [
        t.model_copy(
            update={
                "cluster_id": cluster_of[t.task_id],
                "split": "test" if cluster_of[t.task_id] in split.test_clusters else "dev",
            }
        )
        for t in tasks
    ]
    smoke = select_smoke(tasks, plan.smoke_size, seed)
    problems = check_split(tasks, plan)
    details = suite_details(tw, tasks, originals, faults, accepted, cands, stats, smoke, split)
    return Suite(tasks, smoke, details, problems), sources


def suite_details(
    tw: TaskWorld,
    tasks: list[Task],
    originals: list[Original],
    faults: list[Derived],
    accepted: list[tuple[pp.Source, str]],
    cands: Candidates,
    stats: SelectStats,
    smoke: list[str],
    split: SplitResult,
) -> dict[str, Any]:
    by_group: dict[str, Counter[str]] = defaultdict(Counter)
    for t in tasks:
        by_group[group_of(t)][t.split] += 1
    cluster_split: dict[str, set[str]] = defaultdict(set)
    for t in tasks:
        cluster_split[t.split].add(t.cluster_id)
    originals_by_kind: Counter[str] = Counter(o.draft.kind for o in originals)
    scenario_counts: Counter[str] = Counter(o.draft.scenario.type for o in originals)
    users: Counter[str] = Counter(o.draft.scenario.account_id for o in originals)
    families: Counter[str] = Counter(o.draft.scenario.generator_family for o in originals)
    kinds_by_user: dict[str, Counter[str]] = defaultdict(Counter)
    for o in originals:
        kinds_by_user[o.draft.kind][o.draft.scenario.account_id] += 1
    kinds_by_family: dict[str, Counter[str]] = defaultdict(Counter)
    for o in originals:
        kinds_by_family[o.draft.kind][o.draft.scenario.generator_family] += 1
    return {
        "suite_version": SUITE_VERSION,
        "world_sha256": tw.world_sha256,
        "skeleton_sha256": tw.skeleton_sha256,
        "tasks_by_type": {g: dict(sorted(c.items())) for g, c in sorted(by_group.items())},
        "originals_by_type": dict(sorted(originals_by_kind.items())),
        "multi_turn_conversations": originals_by_kind["multi_turn"],
        "scenarios_by_type": dict(sorted(scenario_counts.items())),
        "originals_by_user": dict(sorted(users.items())),
        "originals_by_family": dict(sorted(families.items())),
        "originals_by_user_and_type": {
            k: dict(sorted(v.items())) for k, v in sorted(kinds_by_user.items())
        },
        "originals_by_family_and_type": {
            k: dict(sorted(v.items())) for k, v in sorted(kinds_by_family.items())
        },
        "clusters": {
            "dev": len(cluster_split["dev"]),
            "test": len(cluster_split["test"]),
            "total": len(cluster_split["dev"] | cluster_split["test"]),
        },
        "split_search": {"cost": split.cost, "attempts": split.attempts},
        "dropped": {
            "builder": dict(sorted(cands.dropped.items())),
            "ref_conflict_scenarios": len(stats.ref_conflicts),
            "duplicate_answer_scenarios": len(stats.duplicate_answers),
        },
        "reject_tasks": sum(1 for t in tasks if t.approval_policy == ["reject"]),
        "must_not_trimmed_tasks": sum(1 for o in originals if o.draft.note == "must_not_trimmed"),
        "fault_tasks": {
            "read": sum(1 for f in faults if f.fault_error != "result_unknown"),
            "write": sum(1 for f in faults if f.fault_error == "result_unknown"),
        },
        "paraphrase_tasks": len(accepted),
        "smoke": {"size": len(smoke)},
    }


NOTES = (
    "종단 과제 v1(ADR-0026 §4). 정답은 skeleton.json의 시나리오 기록과 world.json에서 코드가 다시 "
    "계산했고 불일치하면 생성이 실패한다. 본문은 LLM이 쓴 합성 데이터라 정답 값이 본문에 있는지만 "
    "검사했다(함축된 다른 사실은 사람 검수로 본다, ③). 사용자 질문에서 '메일'은 받은 메일 전체를 "
    "가리킨다. mail_fact의 정답 메일은 최신 값이 적힌 메일 하나이며 "
    "같은 값을 되풀이하는 다른 메일이 "
    "있을 수 있다(검수 항목). 답장·초안 쓰기는 runflow가 `send_mail`(초안 생성 후 발송)만 가지므로 "
    "기대 변화를 `draft_sent`로 둔다. test 봉인은 러너의 확인 플래그 수준이고 공개 저장소라 정답은 "
    "누구나 읽을 수 있다. 장애 주입 파생은 같은 정답을 기대하고, 표현 변형 파생은 원본과 같은 "
    "정답·묶음이다."
)


def manifest_for(
    tw: TaskWorld,
    world: dict[str, Any],
    suite: Suite,
    seed: int,
    sha: str,
    paraphrase_info: dict[str, Any],
) -> Manifest:
    accounts = world["accounts"]
    n = len(suite.tasks)
    dev = sum(1 for t in suite.tasks if t.split == "dev")
    details = {**suite.details, "paraphrase": paraphrase_info}
    return Manifest(
        name="orbit-eval",
        version="v1",
        generator_git_sha=sha,
        seed=seed,
        created_with="structure+llm",
        split_ratio=SplitRatio(dev=round(dev / n, 4), test=round((n - dev) / n, 4)),
        counts=Counts(
            users=len(accounts),
            messages=sum(len(a.get("messages", [])) for a in accounts.values()),
            events=sum(len(a.get("events", [])) for a in accounts.values()),
            tasks=n,
        ),
        notes=NOTES,
        smoke_task_ids=suite.smoke_ids,
        details=details,
    )


def paraphrase_summary(
    enabled: bool,
    sources: Sequence[pp.Source],
    rows_all: list[dict[str, Any]],
    rows: dict[str, dict[str, Any]],
    models: dict[str, FamilyModel] | None,
    used: int,
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "note": "--skip-paraphrase: 표현 변형 파생 문항 없이 생성했다"}
    tried = [rows[s.origin_id] for s in sources if s.origin_id in rows]
    cost_by_family: Counter[str] = Counter()
    for r in rows_all:
        cost_by_family[str(r.get("family"))] += round(float(r.get("cost_usd") or 0) * 1e6)
    return {
        "enabled": True,
        "prompt_version": pp.PROMPT_VERSION,
        "used": used,
        "candidates_tried": len(tried),
        "failed_candidates": sum(1 for r in tried if r.get("status") == "failed"),
        "attempts": sum(int(r.get("attempts") or 0) for r in rows_all),
        "cost_usd": round(pp.total_cost(rows_all), 6),
        "cost_usd_by_family": {f: round(v / 1e6, 6) for f, v in sorted(cost_by_family.items())},
        "models": {
            f: {"model_id": m.model_id, "route": m.route, "temperature": m.temperature}
            for f, m in sorted((models or {}).items())
        },
    }


# ---------------------------------------------------------------- CLI


def _copy_paraphrase(dataset_dir: Path, out: Path) -> None:
    src, dst = dataset_dir / pp.PARAPHRASE_FILE, out / pp.PARAPHRASE_FILE
    if out != dataset_dir and src.exists() and not dst.exists():
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def run(
    dataset_dir: Path,
    seed: int,
    out: Path,
    *,
    skip_paraphrase: bool,
    max_cost_usd: float | None,
    dry_run: bool,
    sha: str,
    plan: SuitePlan = DEFAULT_PLAN,
    models_config: str | None = None,
    openai_route: str | None = None,
    concurrency: int = 4,
    llm_factory: LLMFactory | None = None,
    echo: Callable[[str], None] = print,
) -> int:
    dataset_dir, out = dataset_dir.resolve(), out.resolve()
    tw, world = load_task_world(dataset_dir)
    ctx = Ctx(tw, seed)
    cands = build_candidates(ctx)
    originals, _ = select_originals(cands, plan, seed)
    faults = pick_faults(originals, plan, seed)
    sources = paraphrase_candidates(originals, faults, plan, seed)
    rows: dict[str, dict[str, Any]] = {}
    models: dict[str, FamilyModel] | None = None
    if not skip_paraphrase:
        models = load_models(models_config, openai_route=openai_route)
        cache_path = out / pp.PARAPHRASE_FILE
        if not cache_path.exists():
            cache_path = dataset_dir / pp.PARAPHRASE_FILE
        pp.check_model_configs(pp.read_all_rows(cache_path), models)
        rows = pp.read_rows(cache_path)
        have = accepted_paraphrases(sources, rows, plan.paraphrase_need)
        missing = plan.paraphrase_need - len(have)
        pending = [s for s in sources if not pp.usable_row(rows.get(s.origin_id), s)]
        spent = pp.total_cost(pp.read_all_rows(cache_path))
        echo(
            f"표현 변형: 필요 {plan.paraphrase_need}개 중 확보 {len(have)}개, 남은 "
            f"{missing}개. 누적 지출 ${spent:.4f}"
        )
        if missing > 0:
            ahead = pending[: math.ceil(missing * 1.3)]
            est = pp.estimate_cost(ahead, models)
            for fam, e in est["families"].items():
                echo(
                    f"  {fam}: 원본 {e['sources']}개, 입력 {e['input_tokens']:,} / 출력 "
                    f"{e['output_tokens']:,} 토큰, 예상 ${e['cost_usd']:.4f}"
                )
            echo(f"예상 합계 ${est['total_cost_usd']:.4f} (누적 상한 ${max_cost_usd})")
            if dry_run:
                return EXIT_OK
            _copy_paraphrase(dataset_dir, out)
            if max_cost_usd is None:
                raise SuiteError("표현 변형에는 비용 상한이 필요하다")
            summary = asyncio.run(
                pp.run_paraphrases(
                    sources,
                    plan.paraphrase_need,
                    out,
                    models,
                    pp.ParaphraseConfig(max_cost_usd=max_cost_usd, concurrency=concurrency),
                    llm_factory=llm_factory,
                    progress=echo,
                )
            )
            echo(
                f"요청 {summary.requests}회, 이번 실행 지출 ${summary.spent_usd:.4f}, "
                f"통과 {summary.ok}건, 실패 {summary.failed}건"
            )
            rows = pp.read_rows(out / pp.PARAPHRASE_FILE)
            if summary.stop_reason == "cost_cap":
                echo(f"비용 상한 ${max_cost_usd}에 닿아 멈췄다. 같은 명령으로 이어서 실행한다")
                return EXIT_COST_CAP
            if summary.stop_reason == "fatal":
                print(f"영구 오류로 멈췄다: {summary.fatal_message}", file=sys.stderr)
                return EXIT_FATAL
            have = accepted_paraphrases(sources, rows, plan.paraphrase_need)
            if len(have) < plan.paraphrase_need:
                print(
                    f"표현 변형 후보를 모두 써도 {len(have)}개뿐이다. 후보를 늘려야 한다",
                    file=sys.stderr,
                )
                return EXIT_PLAN
        elif dry_run:
            echo("추가 호출이 필요 없다(예상 비용 $0)")
            return EXIT_OK
        _copy_paraphrase(dataset_dir, out)
    elif dry_run:
        echo("--skip-paraphrase라 LLM 호출이 없다")
        return EXIT_OK

    suite, _ = build_suite(
        tw, seed, plan, rows=rows, include_paraphrase=not skip_paraphrase, candidates=cands
    )
    if suite.problems:
        for p in suite.problems:
            print(f"분할 목표 실패: {p}", file=sys.stderr)
        return EXIT_PLAN
    rows_all = pp.read_all_rows(out / pp.PARAPHRASE_FILE) if not skip_paraphrase else []
    info = paraphrase_summary(
        not skip_paraphrase, sources, rows_all, rows, models, suite.details["paraphrase_tasks"]
    )
    manifest = manifest_for(tw, world, suite, seed, sha, info)
    problems = ds.validate_dataset(manifest, world, suite.tasks)
    if problems:
        for p in problems:
            print(f"검증 실패: {p}", file=sys.stderr)
        return EXIT_PLAN
    out.mkdir(parents=True, exist_ok=True)
    if out != dataset_dir:  # 출력 디렉터리만으로 읽을 수 있게 세계를 함께 둔다
        shutil.copyfile(dataset_dir / "world.json", out / ds.WORLD)
    ds.write_tasks(out, suite.tasks)
    ds.write_manifest(out, manifest)
    d = suite.details
    echo(f"생성 완료: 과제 {len(suite.tasks)}개 -> {out}")
    echo(f"유형·분할별: {json.dumps(d['tasks_by_type'], ensure_ascii=False, sort_keys=True)}")
    echo(f"묶음: {d['clusters']}, 스모크 {len(suite.smoke_ids)}개")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orbit 종단 과제·스모크 생성기(세계 v1)")
    parser.add_argument("--dataset-dir", required=True, help="skeleton.json·world.json이 있는 폴더")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", default=None, help="출력 폴더(기본: --dataset-dir)")
    parser.add_argument("--git-sha", default=None, help="manifest에 적을 SHA(기본: 현재 HEAD)")
    parser.add_argument("--skip-paraphrase", action="store_true", help="표현 변형 파생 없이 생성")
    parser.add_argument(
        "--max-cost-usd", type=float, default=None, help="표현 변형 재개 포함 누적 비용 상한"
    )
    parser.add_argument("--dry-run", action="store_true", help="표현 변형 비용만 추정하고 끝낸다")
    parser.add_argument(
        "--models-config", default=None, help="계열별 모델 설정 TOML(realize와 같은 형식)"
    )
    parser.add_argument("--openai-route", choices=("openai", "openrouter"), default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)
    if not args.skip_paraphrase and (
        args.max_cost_usd is None or not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0
    ):
        print(
            "--max-cost-usd(0보다 큼)가 필요하다. LLM 없이 만들려면 --skip-paraphrase",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.concurrency < 1:
        print("--concurrency는 1 이상이어야 한다", file=sys.stderr)
        return EXIT_USAGE
    dataset_dir = Path(args.dataset_dir)
    out = Path(args.out) if args.out else dataset_dir
    sha = args.git_sha if args.git_sha is not None else git_sha(Path(__file__).parent)
    try:
        return run(
            dataset_dir,
            args.seed,
            out,
            skip_paraphrase=args.skip_paraphrase,
            max_cost_usd=args.max_cost_usd,
            dry_run=args.dry_run,
            sha=sha,
            models_config=args.models_config,
            openai_route=args.openai_route,
            concurrency=args.concurrency,
        )
    except SuiteError as exc:
        print(f"쿼터를 채우지 못했다: {exc}", file=sys.stderr)
        return EXIT_PLAN
    except (TaskGenError, TaskWorldError) as exc:
        print(f"세계와 시나리오 기록이 어긋난다: {exc}", file=sys.stderr)
        return EXIT_PLAN
    except ModelConfigError as exc:
        print(f"모델 설정 오류: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValidationError as exc:
        names = sorted({".".join(str(p) for p in e["loc"]) or "(설정 전체)" for e in exc.errors()})
        print(
            f"LLM 설정 오류 (확인할 항목: {', '.join(names)}). 호출 경로에 맞는 "
            "ORBIT_OPENAI_API_KEY 또는 ORBIT_OPENROUTER_API_KEY가 있는지 확인한다",
            file=sys.stderr,
        )
        return EXIT_USAGE
    except OSError as exc:
        print(f"입출력 오류: {type(exc).__name__}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
