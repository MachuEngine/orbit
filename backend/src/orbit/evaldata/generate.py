"""평가 데이터셋 생성기와 CLI.

    python -m orbit.evaldata.generate --world eval/worlds/orbit-v0.toml --seed 20261111 \\
        --out eval/datasets/orbit-eval/v0

같은 세계 모델·시드·git SHA면 산출물이 바이트 단위로 같다. LLM을 호출하지 않는다.

분할(ADR-0010 2절): 시나리오를 만들 때 dev/test 소유 구역(날짜·프로젝트 주·인물)을 미리 나눠
근거 아티팩트가 겹치지 않게 한다. 생성 뒤에는 근거를 공유하는 과제를 연결 요소로 묶어 한 묶음이
한 split에만 있는지 확인하고, 아니면 생성을 실패시킨다. 정답 지문이 같은 과제는 하나만 남긴다.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orbit.evaldata import timeline
from orbit.evaldata.dataset import Dataset, write_dataset
from orbit.evaldata.gold import TaskParts, build_parts
from orbit.evaldata.records import EventRec, MailRec, TaskSpec
from orbit.evaldata.schema import Counts, Manifest, SplitRatio, Task, Turn
from orbit.evaldata.timeline import SCENARIO_FOR, Builder
from orbit.evaldata.timeutil import to_utc_iso
from orbit.evaldata.worldmodel import WorldModel, load_world_model

TASK_PREFIX = {
    "day_schedule": "day",
    "free_slot": "free",
    "reschedule": "resched",
    "cancelled_exclusion": "cancel",
    "project_week": "proj",
    "person_next": "person",
}
MAX_ROUNDS = 30
SPLITS = ("dev", "test")

NOTES = (
    "v0: 템플릿 문장으로만 생성한 합성 데이터(LLM 미사용). 1차 유형은 읽기 전용 일정 조회. "
    "split_ratio는 D2-3 미결정이라 임시값이다. 사용자는 모두 Asia/Seoul이지만 "
    "v0 날짜 처리는 UTC 기준이라 모든 일정을 평일 09:00~18:00(KST)에 두고 "
    "종일 일정은 만들지 않는다. 월드는 스냅샷 시각 하나의 최종 상태이며 과제의 as_of_time은 "
    "그 이후다. 정답은 expected_answer(구조화된 답)가 기준이고 answer_facts는 보조 지표다. "
    "dev와 test는 정답 근거(일정·메일)를 공유하지 않는다. "
    "test 봉인은 러너의 확인 플래그 수준이며 공개 저장소라 정답은 누구나 읽을 수 있다. "
    "world의 일정 수에는 과제로 선택되지 않은 시나리오(목표 수 초과·무효로 버려진 것)의 "
    "일정과 반복 일정이 포함된다."
)


@dataclass
class BuildStats:
    """마지막 평가에서 버려진 명세 수(사유별), 분할 묶음 수, split별 과제 수."""

    discarded: Counter[str] = field(default_factory=Counter)
    components: int = 0
    per_split: Counter[str] = field(default_factory=Counter)
    per_type_split: Counter[str] = field(default_factory=Counter)


def git_sha(cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _rng(seed: int, label: str) -> random.Random:
    return random.Random(f"{seed}:{label}")  # noqa: S311  재현성용 의사난수


def _refs(tags: list[str]) -> set[str]:
    return {t for t in tags if t.startswith(("ref_event:", "ref_message:"))}


def _fingerprint(spec: TaskSpec, parts: TaskParts) -> str:
    answer = parts.gold.expected_answer
    payload = answer.model_dump(mode="json") if answer is not None else None
    return f"{spec.kind}|{json.dumps(payload, sort_keys=True, ensure_ascii=False)}"


def _split_targets(wm: WorldModel) -> dict[str, dict[str, int]]:
    """유형별 split 목표 수. dev 비율로 반올림하고 양쪽에 최소 1개를 둔다."""
    out: dict[str, dict[str, int]] = {}
    for kind in SCENARIO_FOR:
        n = getattr(wm.targets, kind)
        n_dev = min(max(round(n * wm.meta.dev_ratio), 1), n - 1)
        out[kind] = {"dev": n_dev, "test": n - n_dev}
    return out


def _evaluate(
    b: Builder, specs: list[TaskSpec], seed: int, targets: dict[str, dict[str, int]]
) -> tuple[dict[tuple[str, str], list[tuple[TaskSpec, TaskParts]]], Counter[str]]:
    """현재 캘린더로 모든 명세를 다시 계산해 유효한 것만 (유형, split)별로 목표 수까지 남긴다.

    버리는 경우: 정답이 비었거나 모호함, 정답 지문 중복, 다른 split의 과제와 근거 공유, 목표 초과.
    """
    kept: dict[tuple[str, str], list[tuple[TaskSpec, TaskParts]]] = {
        (k, s): [] for k in targets for s in SPLITS
    }
    dropped: Counter[str] = Counter()
    seen: set[str] = set()
    owner: dict[str, str] = {}
    for spec in specs:
        if len(kept[(spec.kind, spec.split)]) >= targets[spec.kind][spec.split]:
            dropped["목표 수 초과"] += 1
            continue
        rng = _rng(seed, f"{spec.scenario_id}:{spec.kind}")
        parts = build_parts(spec, b.events, [], b.wm, rng)
        if isinstance(parts, str):
            dropped[parts] += 1
            continue
        fingerprint = _fingerprint(spec, parts)
        if fingerprint in seen:
            dropped["정답 지문 중복"] += 1
            continue
        refs = _refs(parts.tags)
        if any(owner.get(r, spec.split) != spec.split for r in refs):
            dropped["다른 split과 근거 공유"] += 1
            continue
        seen.add(fingerprint)
        owner.update({r: spec.split for r in refs})
        kept[(spec.kind, spec.split)].append((spec, parts))
    return kept, dropped


def _build_specs(b: Builder, seed: int) -> tuple[list[TaskSpec], Counter[str]]:
    wm = b.wm
    targets = _split_targets(wm)
    # 동명이인 인물이 속한 프로젝트로 인물 과제를 먼저 만든다 (인물의 split 시나리오가 사용한다).
    for person in wm.people:
        if wm.is_homonym(person):
            for project in wm.projects:
                if person.key in project.members:
                    b.pp_overrides.append((project, person))
                    break
    specs: list[TaskSpec] = []
    need = {k: dict(v) for k, v in targets.items()}
    for _ in range(MAX_ROUNDS):
        for scenario in ("agenda", "reschedule", "cancel", "projperson"):
            for split in SPLITS:
                count = max(
                    (need[k][split] for k, s in SCENARIO_FOR.items() if s == scenario), default=0
                )
                if count > 0:
                    specs.extend(timeline.run_scenarios(b, scenario, count, split))
        kept, dropped = _evaluate(b, specs, seed, targets)
        need = {k: {s: targets[k][s] - len(kept[(k, s)]) for s in SPLITS} for k in targets}
        if all(n <= 0 for per in need.values() for n in per.values()):
            return [spec for key in sorted(kept) for spec, _ in kept[key]], dropped
    raise RuntimeError(f"목표 과제 수를 채우지 못했다: {need}")


def _components(tasks: list[Task]) -> list[list[Task]]:
    """근거(ref_event·ref_message)를 하나라도 공유하는 과제를 묶는다 (union-find)."""
    parent = list(range(len(tasks)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[str, int] = {}
    for i, t in enumerate(tasks):
        for ref in _refs(t.tags):
            if ref in first:
                parent[find(i)] = find(first[ref])
            else:
                first[ref] = i
    groups: dict[int, list[Task]] = {}
    for i, t in enumerate(tasks):
        groups.setdefault(find(i), []).append(t)
    return list(groups.values())


def _contact(c: tuple[str, str]) -> dict[str, str]:
    return {"email": c[1], "name": c[0]}


def _event_json(b: Builder, ev: EventRec) -> dict[str, Any]:
    user = b.wm.user(ev.user)
    me = {**_contact((user.name, user.email)), "response_status": "accepted"}
    people = [{**_contact(b.contact(k)), "response_status": "accepted"} for k in ev.attendees]
    org = b.contact(ev.organizer)
    return {
        "id": ev.id,
        "calendar_id": user.calendar_id,
        "title": ev.title,
        "description": ev.description,
        "location": ev.location,
        "start": to_utc_iso(ev.start),
        "end": to_utc_iso(ev.end),
        "all_day": False,
        "attendees": [*people, me],
        "organizer": {**_contact(org), "response_status": "accepted"},
        "status": ev.status,
        "version": ev.version,
        "updated_at": to_utc_iso(ev.updated_at),
    }


def _mail_json(m: MailRec) -> dict[str, Any]:
    first_line = " ".join(m.body.split())
    return {
        "id": m.id,
        "thread_id": m.thread,
        "from": _contact(m.sender),
        "to": [_contact(c) for c in m.to],
        "cc": [],
        "subject": m.subject,
        "body_text": m.body,
        "snippet": first_line[:60],
        "sent_at": to_utc_iso(m.sent_at),
        "labels": m.labels,
        "attachments": [],
        "headers": {},
    }


def _world_json(b: Builder, seed: int) -> dict[str, Any]:
    accounts: dict[str, Any] = {}
    for user in b.wm.users:
        events = sorted(b.user_events(user.key), key=lambda e: (e.start, e.id))
        mails = sorted((m for m in b.mails if m.user == user.key), key=lambda m: (m.sent_at, m.id))
        threads: dict[str, list[str]] = {}
        for m in mails:
            threads.setdefault(m.thread, []).append(m.id)
        accounts[user.account_id] = {
            "email": user.email,
            "calendars": [
                {
                    "id": user.calendar_id,
                    "name": user.calendar_name,
                    "time_zone": "Asia/Seoul",
                    "primary": True,
                }
            ],
            "events": [_event_json(b, e) for e in events],
            "messages": [_mail_json(m) for m in mails],
            "threads": [{"thread_id": t, "message_ids": ids} for t, ids in sorted(threads.items())],
            "drafts": [],
        }
    return {"schema_version": 1, "seed": seed, "accounts": accounts}


def build_dataset(wm: WorldModel, seed: int, sha: str) -> tuple[Dataset, BuildStats]:
    b = Builder(wm, _rng(seed, "timeline"))
    b.add_recurring()
    specs, dropped = _build_specs(b, seed)
    timeline.add_noise(b)

    counters: dict[str, int] = {}
    tasks: list[Task] = []
    for spec in specs:
        rng = _rng(seed, f"{spec.scenario_id}:{spec.kind}")
        parts = build_parts(spec, b.events, b.mails, wm, rng)
        if isinstance(parts, str):
            raise RuntimeError(f"노이즈 추가 뒤 명세가 무효가 됐다: {spec.scenario_id} ({parts})")
        counters[spec.kind] = counters.get(spec.kind, 0) + 1
        tasks.append(
            Task(
                task_id=f"{TASK_PREFIX[spec.kind]}-{counters[spec.kind]:03d}",
                scenario_id=spec.scenario_id,
                split=spec.split,
                as_of_time=spec.as_of,
                account_id=wm.user(spec.user).account_id,
                turns=[Turn(role="user", content=parts.question)],
                gold=parts.gold,
                difficulty=parts.difficulty,
                tags=parts.tags,
            )
        )
    tasks.sort(key=lambda t: t.task_id)

    groups = _components(tasks)
    for group in groups:
        if len({t.split for t in group}) > 1:
            ids = ", ".join(t.task_id for t in group)
            raise RuntimeError(f"근거를 공유하는 과제 묶음이 dev와 test에 걸쳐 있다: {ids}")
    if len(groups) < 2 or {t.split for t in tasks} != set(SPLITS):
        raise RuntimeError("과제 묶음이 하나로 뭉쳐 dev/test 분할이 불가능하다")

    stats = BuildStats(dropped, len(groups))
    for t in tasks:
        stats.per_split[t.split] += 1
        stats.per_type_split[f"{t.tags[0][5:]}/{t.split}"] += 1

    world = _world_json(b, seed)
    manifest = Manifest(
        name=wm.meta.dataset_name,
        version=wm.meta.dataset_version,
        generator_git_sha=sha,
        seed=seed,
        created_with="templates",
        split_ratio=SplitRatio(dev=wm.meta.dev_ratio, test=wm.meta.test_ratio),
        counts=Counts(
            users=len(wm.users), messages=len(b.mails), events=len(b.events), tasks=len(tasks)
        ),
        notes=f"{NOTES} 분할 묶음(근거 공유 과제 연결 요소)은 {len(groups)}개다.",
    )
    return Dataset(manifest, world, tasks), stats


def generate(
    world_path: str | Path, seed: int, out: str | Path, sha: str | None = None
) -> tuple[Dataset, BuildStats]:
    wm = load_world_model(world_path)
    dataset, stats = build_dataset(
        wm, seed, sha if sha is not None else git_sha(Path(__file__).parent)
    )
    write_dataset(out, dataset.manifest, dataset.world_dict, dataset.tasks)
    return dataset, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orbit 평가 데이터셋 생성 (템플릿, LLM 없음)")
    parser.add_argument("--world", required=True, help="세계 모델 TOML")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True, help="출력 데이터셋 디렉터리")
    parser.add_argument("--git-sha", default=None, help="manifest에 적을 SHA (기본: 현재 HEAD)")
    args = parser.parse_args(argv)
    ds, stats = generate(args.world, args.seed, args.out, args.git_sha)
    c = ds.manifest.counts
    print(f"생성 완료: 메일 {c.messages}, 일정 {c.events}, 과제 {c.tasks} -> {args.out}")
    print(f"split별 과제: {dict(stats.per_split)}, 분할 묶음 {stats.components}개")
    print(f"유형/split별: {dict(sorted(stats.per_type_split.items()))}")
    print(f"마지막 평가에서 버려진 명세: {dict(stats.discarded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
