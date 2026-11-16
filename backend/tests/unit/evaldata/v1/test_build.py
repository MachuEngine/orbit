"""작은 세계 모델로 구조 생성기를 돌린 결과: 결정성, 시나리오 유형, 근거, 본문 명세."""

import json
from collections import Counter
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbit.evaldata.v1.build import (
    SCENARIO_FUNCS,
    build_skeleton,
    file_sha256,
    generate,
    skeleton_json,
)
from orbit.evaldata.v1.facts import fact_in
from orbit.evaldata.v1.plan import BUILD_ORDER
from orbit.evaldata.v1.skeleton import (
    SCENARIO_TYPES,
    AllDayTruth,
    MailFactTruth,
    Skeleton,
    TzEventTruth,
)
from orbit.evaldata.v1.stats import refs_in
from orbit.evaldata.v1.worldmodel import WorldModel

from .conftest import SEED, SHA, SMALL_WORLD


def test_every_scenario_type_has_function_and_order() -> None:
    assert set(SCENARIO_FUNCS) == set(SCENARIO_TYPES) == set(BUILD_ORDER)


def test_same_seed_is_byte_identical(small_wm: WorldModel, small_sk: Skeleton) -> None:
    again = build_skeleton(small_wm, SEED, world_sha256=file_sha256(SMALL_WORLD), git_sha_=SHA)
    assert skeleton_json(again) == skeleton_json(small_sk)


def test_different_seed_differs(small_wm: WorldModel, small_sk: Skeleton) -> None:
    other = build_skeleton(small_wm, SEED + 1, world_sha256="x", git_sha_=SHA)
    assert skeleton_json(other) != skeleton_json(small_sk)


def test_cli_writes_outputs(tmp_path: Path) -> None:
    sk = generate(SMALL_WORLD, 5, tmp_path / "out", SHA, template_world=True)
    assert (tmp_path / "out" / "skeleton.json").read_text("utf-8") == skeleton_json(sk)
    stats = json.loads((tmp_path / "out" / "skeleton-stats.json").read_text("utf-8"))
    assert stats["scenarios_total"] == len(sk.scenarios)
    assert (tmp_path / "out" / "world.template.json").exists()


def test_scenario_counts_match_targets(small_wm: WorldModel, small_sk: Skeleton) -> None:
    got = Counter(s.type for s in small_sk.scenarios)
    assert dict(got) == small_wm.targets.scenarios


@pytest.mark.parametrize("type_", SCENARIO_TYPES)
def test_each_type_is_built_with_evidence(type_: str, small_sk: Skeleton) -> None:
    mine = [s for s in small_sk.scenarios if s.type == type_]
    assert mine
    for s in mine:
        assert s.truth.type == type_
        if type_ != "absent":
            assert s.evidence
        else:
            assert not s.evidence and s.distractors


def test_evidence_is_not_shared_between_scenarios(small_sk: Skeleton) -> None:
    seen: Counter[tuple[str, str, str]] = Counter()
    for s in small_sk.scenarios:
        for r in s.evidence:
            seen[(s.account_id, r.kind, r.id)] += 1
    assert seen and max(seen.values()) == 1


def test_all_refs_resolve_to_artifacts(small_sk: Skeleton) -> None:
    for s in small_sk.scenarios:
        a = small_sk.accounts[s.account_id]
        events = {(e.calendar_id, e.id) for e in a.events}
        msgs = {m.id for m in a.messages}
        files = {f.id for f in a.files}
        for r in [*s.evidence, *s.distractors, *refs_in(s.truth)]:
            ok = {
                "event": (r.calendar_id, r.id) in events,
                "message": r.id in msgs,
                "file": r.id in files,
            }
            assert ok[r.kind], (s.scenario_id, r)


def test_template_text_satisfies_facts(small_sk: Skeleton) -> None:
    with_facts = 0
    for spec in small_sk.bodies.values():
        for f in spec.required_facts:
            with_facts += 1
            assert fact_in(spec.template_text, f)
        for f in spec.forbidden_facts:
            assert not fact_in(spec.template_text, f)
    assert with_facts > 20


def test_body_specs_have_no_text_and_valid_lengths(small_sk: Skeleton) -> None:
    for spec in small_sk.bodies.values():
        if spec.artifact == "document":
            assert spec.length_class != "short"
        assert spec.target_chars[0] < spec.target_chars[1]
    mails = [s for s in small_sk.bodies.values() if s.artifact == "mail"]
    c = Counter(s.length_class for s in mails)
    assert abs(c["short"] / len(mails) - 0.4) <= 0.03
    assert abs(c["long"] / len(mails) - 0.2) <= 0.03


def test_family_assignment_is_balanced_and_scenario_consistent(small_sk: Skeleton) -> None:
    c = Counter(b.generator_family for b in small_sk.bodies.values())
    assert set(c) == {"openai", "anthropic", "google"}
    n = sum(c.values())
    assert all(abs(v - n / 3) <= max(1.0, 0.05 * n) for v in c.values())
    for s in small_sk.scenarios:
        for r in [*s.evidence, *s.distractors]:
            if r.body_id:
                assert small_sk.bodies[r.body_id].generator_family == s.generator_family


def test_mail_fact_has_latest_and_previous_values(small_sk: Skeleton) -> None:
    facts = [s.truth for s in small_sk.scenarios if isinstance(s.truth, MailFactTruth)]
    assert {t.fact_kind for t in facts} == {"deadline", "amount", "place", "decision", "attachment"}
    for t in facts:
        assert t.latest.value != t.previous.value
        assert t.latest_message.id != t.previous_message.id


def test_all_day_events_start_at_calendar_midnight(small_sk: Skeleton) -> None:
    """종일 일정은 그 캘린더 시간대 00:00~다음 날 00:00이다(ADR-0027). 한계 표시는 남기지 않는다."""
    truths = [s.truth for s in small_sk.scenarios if isinstance(s.truth, AllDayTruth)]
    assert truths and all(t.contract_caveat is None for t in truths)
    assert all("contract_caveat" not in t.model_dump(mode="json") for t in truths)
    zones = {a.time_zone for a in small_sk.accounts.values()}
    assert "America/Los_Angeles" in zones  # UTC와 다른 시간대 사용자가 있다
    n = 0
    for a in small_sk.accounts.values():
        for e in a.events:
            if e.all_day:
                n += 1
                zone = ZoneInfo(next(c.time_zone for c in a.calendars if c.id == e.calendar_id))
                start, end = e.start.astimezone(zone), e.end.astimezone(zone)
                assert start.time() == end.time() == time(0, 0)
                assert (end.date() - start.date()).days == 1
    assert n
    for t in truths:
        zone = ZoneInfo(t.time_zone)
        assert t.span.start.astimezone(zone).date() == t.intended_date


def test_tz_event_covers_local_and_foreign(small_sk: Skeleton) -> None:
    truths = [s.truth for s in small_sk.scenarios if isinstance(s.truth, TzEventTruth)]
    assert {t.variant for t in truths} == {"local", "foreign_attendee"}
    for t in truths:
        if t.variant == "local":
            assert t.user_time_zone == "America/Los_Angeles"
    # 일광 절약 시간 종료(2026-11-01) 전(-7)과 후(-8) 일정이 모두 있다
    assert {t.user_utc_offset_hours for t in truths if t.variant == "local"} == {-7.0, -8.0}


def test_search_mailboxes_are_nested_subsets(small_wm: WorldModel, small_sk: Skeleton) -> None:
    accts = [u.account_id for u in small_wm.search_users()]
    sets = [{m.body_id for m in small_sk.accounts[a].messages} for a in accts]
    assert [len(small_sk.accounts[a].messages) for a in accts] == small_wm.targets.search_sizes
    assert sets[0] < sets[1] < sets[2]
    for a in accts:
        mb = small_sk.search_mailboxes[a]
        assert mb.size == len(small_sk.accounts[a].messages)
        assert set(mb.message_by_body) == {m.body_id for m in small_sk.accounts[a].messages}
    assert small_sk.search_mailboxes[accts[0]].subset_of == accts[1]


def test_no_real_world_text_in_addresses(small_sk: Skeleton) -> None:
    emails = {p.email for p in small_sk.people.values()}
    for a in small_sk.accounts.values():
        emails.add(a.email)
        emails.update(m.from_.email for m in a.messages)
    assert all(
        e.endswith((".example.com", ".example.org", ".example.net", ".test", "example.org"))
        for e in emails
    )
