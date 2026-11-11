"""평가 데이터셋 생성기와 CLI.

    python -m orbit.evaldata.generate --world eval/worlds/orbit-v0.toml --seed 20261111 \\
        --out eval/datasets/orbit-eval/v0

같은 세계 모델·시드·git SHA면 산출물이 바이트 단위로 같다. LLM을 호출하지 않는다.
"""

from __future__ import annotations

import argparse
import random
import subprocess
from pathlib import Path
from typing import Any

from orbit.evaldata import timeline
from orbit.evaldata.dataset import Dataset, write_dataset
from orbit.evaldata.gold import build_parts
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

NOTES = (
    "v0: 템플릿 문장으로만 생성한 합성 데이터(LLM 미사용). 1차 유형은 읽기 전용 일정 조회. "
    "split_ratio는 D2-3 미결정이라 임시값이다. 사용자는 모두 Asia/Seoul이지만 "
    "v0 날짜 처리는 UTC 기준이라 "
    "모든 일정을 평일 09:00~18:00(KST)에 두고 종일 일정은 만들지 않는다. "
    "월드는 스냅샷 시각 하나의 최종 상태이며 과제의 as_of_time은 그 이후다."
)


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


def _evaluate(
    b: Builder, specs: list[TaskSpec], seed: int, targets: dict[str, int]
) -> dict[str, list[tuple[TaskSpec, Any]]]:
    """현재 캘린더로 모든 명세를 다시 계산해 유효한 것만 유형별로 목표 수까지 남긴다."""
    kept: dict[str, list[tuple[TaskSpec, Any]]] = {k: [] for k in targets}
    for spec in specs:
        if len(kept[spec.kind]) >= targets[spec.kind]:
            continue
        rng = _rng(seed, f"{spec.scenario_id}:{spec.kind}")
        parts = build_parts(spec, b.events, [], b.wm, rng)
        if parts is not None:
            kept[spec.kind].append((spec, parts))
    return kept


def _build_specs(b: Builder, seed: int) -> list[TaskSpec]:
    wm = b.wm
    targets = {k: getattr(wm.targets, k) for k in SCENARIO_FOR}
    # 동명이인 인물이 속한 프로젝트로 인물 과제를 먼저 만든다.
    for person in wm.people:
        if wm.is_homonym(person):
            for project in wm.projects:
                if person.key in project.members:
                    b.pp_overrides.append((project, person))
                    break
    specs: list[TaskSpec] = []
    need = dict(targets)
    for _ in range(MAX_ROUNDS):
        for scenario in ("agenda", "reschedule", "cancel", "projperson"):
            count = max((need[k] for k, s in SCENARIO_FOR.items() if s == scenario), default=0)
            if count > 0:
                specs.extend(timeline.run_scenarios(b, scenario, count))
        kept = _evaluate(b, specs, seed, targets)
        need = {k: targets[k] - len(kept[k]) for k in targets}
        if all(n <= 0 for n in need.values()):
            return [spec for k in targets for spec, _ in kept[k]]
    raise RuntimeError(f"목표 과제 수를 채우지 못했다: {need}")


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


def _assign_splits(scenarios: list[str], dev_ratio: float, seed: int) -> dict[str, str]:
    """시나리오 종류별로 섞어 dev/test를 나눈다. 같은 시나리오는 같은 쪽이다."""
    by_kind: dict[str, list[str]] = {}
    for sid in sorted(set(scenarios)):
        by_kind.setdefault(sid.split("-")[1], []).append(sid)
    rng = _rng(seed, "split")
    out: dict[str, str] = {}
    for kind in sorted(by_kind):
        ids = by_kind[kind]
        rng.shuffle(ids)
        n_dev = round(len(ids) * dev_ratio)
        if len(ids) >= 2:
            n_dev = min(max(n_dev, 1), len(ids) - 1)
        for i, sid in enumerate(ids):
            out[sid] = "dev" if i < n_dev else "test"
    return out


def build_dataset(wm: WorldModel, seed: int, sha: str) -> Dataset:
    b = Builder(wm, _rng(seed, "timeline"))
    b.add_recurring()
    specs = _build_specs(b, seed)
    timeline.add_noise(b)

    splits = _assign_splits([s.scenario_id for s in specs], wm.meta.dev_ratio, seed)
    counters: dict[str, int] = {}
    tasks: list[Task] = []
    for spec in specs:
        rng = _rng(seed, f"{spec.scenario_id}:{spec.kind}")
        parts = build_parts(spec, b.events, b.mails, wm, rng)
        if parts is None:
            raise RuntimeError(f"소음 추가 뒤 명세가 무효가 됐다: {spec.scenario_id} {spec.kind}")
        counters[spec.kind] = counters.get(spec.kind, 0) + 1
        tasks.append(
            Task(
                task_id=f"{TASK_PREFIX[spec.kind]}-{counters[spec.kind]:03d}",
                scenario_id=spec.scenario_id,
                split=splits[spec.scenario_id],
                as_of_time=spec.as_of,
                account_id=wm.user(spec.user).account_id,
                turns=[Turn(role="user", content=parts.question)],
                gold=parts.gold,
                difficulty=parts.difficulty,
                tags=parts.tags,
            )
        )
    tasks.sort(key=lambda t: t.task_id)
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
        notes=NOTES,
    )
    return Dataset(manifest, world, tasks)


def generate(world_path: str | Path, seed: int, out: str | Path, sha: str | None = None) -> Dataset:
    wm = load_world_model(world_path)
    dataset = build_dataset(wm, seed, sha if sha is not None else git_sha(Path(__file__).parent))
    write_dataset(out, dataset.manifest, dataset.world_dict, dataset.tasks)
    return dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orbit 평가 데이터셋 생성 (템플릿, LLM 없음)")
    parser.add_argument("--world", required=True, help="세계 모델 TOML")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True, help="출력 데이터셋 디렉터리")
    parser.add_argument("--git-sha", default=None, help="manifest에 적을 SHA (기본: 현재 HEAD)")
    args = parser.parse_args(argv)
    ds = generate(args.world, args.seed, args.out, args.git_sha)
    c = ds.manifest.counts
    print(f"생성 완료: 메일 {c.messages}, 일정 {c.events}, 과제 {c.tasks} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
