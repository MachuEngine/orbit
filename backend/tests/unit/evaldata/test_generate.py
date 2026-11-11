"""생성기 산출물의 결정성, 규모, 분할, 합성 데이터 규칙."""

import json
import random
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.dataset import Dataset, evidence_clusters, load_dataset
from orbit.evaldata.generate import generate
from orbit.evaldata.schema import Gold, Manifest, Task
from orbit.evaldata.timeline import Builder
from orbit.evaldata.timeutil import KST, kor_when, time_forms
from orbit.evaldata.worldmodel import load_world_model

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
    assert 55 <= c.events <= 110
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
    assert (counts["dev"], counts["test"]) == (36, 24)  # dev 비율 0.6
    # 유형마다 dev와 test에 모두 과제가 있다
    by_type = {(t.tags[0], t.split) for t in ds.tasks}
    assert len(by_type) == 12


def test_task_contract_fields() -> None:
    assert list(Task.model_fields) == [
        "task_id",
        "scenario_id",
        "cluster_id",
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
        "expected_answer",
        "must_not_contain",
        "expected_changes",
        "required_tool_calls",
        "forbidden_tools",
    ]


EXPECTED_KIND = {
    "day_schedule": "event_set",
    "cancelled_exclusion": "event_set",
    "project_week": "event_set",
    "person_next": "event",
    "reschedule": "event_time",
    "free_slot": "time_slot",
}


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
        assert g.required_tool_calls == []
        assert g.must_not_contain == []
        assert g.answer_facts
        assert g.expected_answer is not None
        assert g.expected_answer.kind == EXPECTED_KIND[t.tags[0].split(":")[1]]
        assert t.as_of_time.utcoffset() is not None
        assert t.turns[0].role == "user"


def test_expected_answer_serialization(dataset_dir: Path) -> None:
    """시각은 오프셋이 있는 ISO 8601이고 일정 참조는 {calendar_id, event_id}다."""
    for line in (dataset_dir / "tasks.jsonl").read_text("utf-8").splitlines():
        answer = json.loads(line)["gold"]["expected_answer"]
        for key in ("start",):
            if key in answer:
                assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+09:00", answer[key])
        refs = answer.get("events", [answer["event"]] if "event" in answer else [])
        assert all(set(r) == {"calendar_id", "event_id"} for r in refs)


def test_cancelled_events_are_evidence_but_not_answer(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    events = {e["id"]: e for a in ds.world_dict["accounts"].values() for e in a["events"]}
    cancelled = [t for t in ds.tasks if t.tags[0] == "type:cancelled_exclusion"]
    assert cancelled
    for t in cancelled:
        answer = t.gold.expected_answer
        assert answer is not None and answer.kind == "event_set"
        answered = {r.event_id for r in answer.events}
        refs = {x.split(":", 1)[1] for x in t.tags if x.startswith("ref_event:")}
        gone = {i for i in refs if events[i]["status"] == "cancelled"}
        assert gone and not gone & answered and answered <= refs


def _refs(t: Task) -> set[str]:
    return {x for x in t.tags if x.startswith(("ref_event:", "ref_message:"))}


def test_no_shared_evidence_between_splits(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    owner: dict[str, str] = {}
    for t in ds.tasks:
        assert _refs(t)
        for ref in _refs(t):
            assert owner.setdefault(ref, t.split) == t.split, ref


def test_no_duplicate_answers(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    prints = [
        (t.tags[0], json.dumps(t.gold.expected_answer.model_dump(mode="json"), sort_keys=True))
        for t in ds.tasks
        if t.gold.expected_answer is not None
    ]
    assert len(set(prints)) == len(prints) == 60


def test_evidence_is_complete(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    for t in ds.tasks:
        acct = ds.world_dict["accounts"][t.account_id]
        refs = {x.split(":", 1)[1] for x in t.tags if x.startswith("ref_event:")}
        msgs = {x.split(":", 1)[1] for x in t.tags if x.startswith("ref_message:")}
        kind = t.tags[0]
        if kind in ("type:free_slot", "type:day_schedule", "type:cancelled_exclusion"):
            day = next(x.split(":", 1)[1] for x in t.tags if x.startswith("date:"))
            same_day = {
                e["id"]
                for e in acct["events"]
                if datetime.fromisoformat(e["start"]).astimezone(KST).date().isoformat() == day
            }
            if kind == "type:free_slot":
                assert refs == same_day  # 그날 업무 시간 안의 모든 일정(취소 포함)
            else:
                assert refs <= same_day
        if kind == "type:reschedule":
            for th in acct["threads"]:
                if msgs & set(th["message_ids"]):
                    assert set(th["message_ids"]) <= msgs  # 관련 메일 전부


def test_homonym_company_is_visible_in_data(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    wm = load_world_model(WORLD_MODEL)
    by_email = {p.email: p for p in wm.people}
    homonym_tasks = [t for t in ds.tasks if int(t.difficulty.get("homonym", 0)) == 1]
    assert homonym_tasks
    for t in homonym_tasks:
        (email,) = [x.split(":", 1)[1] for x in t.tags if x.startswith("attendee:")]
        person = by_email[email]
        others = [p for p in wm.people if p.name == person.name and p.email != email]
        assert others
        assert person.company in t.turns[0].content
        mails = ds.world_dict["accounts"][t.account_id]["messages"]
        mine = [m for m in mails if m["from"]["email"] == email]
        assert any(person.company in m["body_text"] for m in mine)
        for other in others:
            theirs = [m for m in mails if m["from"]["email"] == other.email]
            assert not any(person.company in m["body_text"] + m["subject"] for m in theirs)


def test_reschedule_last_confirmation_matches_calendar(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    pattern = re.compile(r"\d+월 \d+일\([월화수목금토일]\)")
    checked = 0
    for t in ds.tasks:
        if t.tags[0] != "type:reschedule":
            continue
        acct = ds.world_dict["accounts"][t.account_id]
        events = {e["id"]: e for e in acct["events"]}
        (event_id,) = [x.split(":", 1)[1] for x in t.tags if x.startswith("ref_event:")]
        ids = {x.split(":", 1)[1] for x in t.tags if x.startswith("ref_message:")}
        mails = sorted(
            (m for m in acct["messages"] if m["id"] in ids), key=lambda m: (m["sent_at"], m["id"])
        )
        timed = [m for m in mails if pattern.search(m["body_text"])]
        start = datetime.fromisoformat(events[event_id]["start"])
        assert kor_when(start) in timed[-1]["body_text"]
        checked += 1
    assert checked == 12


def _local_events(ds: Dataset, account: str) -> list[dict[str, Any]]:
    out = []
    for e in ds.world_dict["accounts"][account]["events"]:
        out.append(
            {
                **e,
                "s": datetime.fromisoformat(e["start"]).astimezone(KST),
                "e": datetime.fromisoformat(e["end"]).astimezone(KST),
            }
        )
    return out


def test_half_day_tasks_have_no_noon_straddle(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    checked = 0
    for t in ds.tasks:
        half = [x.split(":", 1)[1] for x in t.tags if x.startswith("half:")]
        if not half or half[0] == "all":
            continue
        day = date.fromisoformat(next(x.split(":", 1)[1] for x in t.tags if x.startswith("date:")))
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=KST)
        for e in _local_events(ds, t.account_id):
            if e["s"].date() == day:
                assert not e["s"] < noon < e["e"]
        checked += 1
    assert checked > 0


def test_person_next_has_no_in_progress_meeting(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    for t in ds.tasks:
        if t.tags[0] != "type:person_next":
            continue
        (email,) = [x.split(":", 1)[1] for x in t.tags if x.startswith("attendee:")]
        for e in _local_events(ds, t.account_id):
            if e["status"] == "confirmed" and any(a["email"] == email for a in e["attendees"]):
                assert not e["s"] <= t.as_of_time < e["e"]


def test_manifest_notes_state_limits(dataset_dir: Path) -> None:
    notes = load_dataset(dataset_dir).manifest.notes
    assert "test 봉인은 러너의 확인 플래그 수준" in notes
    assert "공개 저장소" in notes
    assert "선택되지 않은 시나리오" in notes
    assert "배경 아티팩트" in notes
    assert "도구 사용을 잰다" in notes
    assert "12:00인 일정은 오후" in notes
    assert re.search(r"분할 묶음.*\d+개\(dev \d+, test \d+\)", notes)


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


def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311  재현성용 의사난수


def test_cluster_ids(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    groups = evidence_clusters(ds.tasks)
    # 현재 데이터(seed 20261111)의 묶음 수. 생성기·월드 모델을 바꾸면 manifest.notes와 함께 고친다.
    assert len(groups) == 15
    assert Counter(g[0].split for g in groups) == {"dev": 6, "test": 9}
    for group in groups:
        expected = "c-" + min(t.task_id for t in group)
        assert {t.cluster_id for t in group} == {expected}
        assert len({t.split for t in group}) == 1
    # 묶음 수는 manifest.notes와 같다
    m = re.search(r"(\d+)개\(dev (\d+), test (\d+)\)", ds.manifest.notes)
    assert m is not None
    per = Counter(g[0].split for g in groups)
    assert (int(m[1]), int(m[2]), int(m[3])) == (len(groups), per["dev"], per["test"])


def test_balance_per_split(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    for split in ("dev", "test"):
        mine = [t for t in ds.tasks if t.split == split]
        assert any(int(t.difficulty.get("homonym", 0)) == 1 for t in mine)
        assert any(
            t.tags[0] == "type:reschedule" and int(t.difficulty["schedule_changes"]) >= 2
            for t in mine
        )


def test_person_split_matches_day_owner(dataset_dir: Path) -> None:
    """비배경 인물이 참석하는 모든 일정의 날짜 소유 split은 그 인물의 split과 같다."""
    ds = load_dataset(dataset_dir)
    wm = load_world_model(WORLD_MODEL)
    b = Builder(wm, _rng(), SEED)
    by_email = {p.email: p for p in wm.people}
    user_of = {u.account_id: u.key for u in wm.users}
    checked = 0
    for acct_id, acct in ds.world_dict["accounts"].items():
        for e in acct["events"]:
            day = datetime.fromisoformat(e["start"]).astimezone(KST).date()
            for a in e["attendees"]:
                person = by_email.get(a["email"])
                if person is None or person.split == "background":
                    continue
                assert b.day_owner(user_of[acct_id], day) == person.split, e["id"]
                checked += 1
    assert checked > 0


def test_day_owner_is_seeded_and_balanced() -> None:
    wm = load_world_model(WORLD_MODEL)
    a = Builder(wm, _rng(), 1)
    b = Builder(wm, _rng(), 1)
    c = Builder(wm, _rng(), 2)
    owners = [[x.day_owner(u.key, d) for u in wm.users for d in x.days] for x in (a, b, c)]
    assert owners[0] == owners[1] != owners[2]
    dev = sum(1 for o in owners[0] if o == "dev")
    assert 0.5 <= dev / len(owners[0]) <= 0.7


def test_relative_date_expressions_cover_both_splits(dataset_dir: Path) -> None:
    """강제는 아니다. 어느 split에 어떤 표현이 없는지 알 수 있게 실패 메시지로 드러낸다."""
    ds = load_dataset(dataset_dir)
    seen = {
        (t.split, str(t.difficulty["relative_date"]).split(" ")[0])
        for t in ds.tasks
        if "relative_date" in t.difficulty
    }
    assert {s for s, _ in seen} == {"dev", "test"}
