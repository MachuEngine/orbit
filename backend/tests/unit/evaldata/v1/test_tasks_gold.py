"""종단 과제 정답 계산 테스트 (ADR-0026 §4). 커밋된 세계 v1로 유형마다 정답을 다시 계산해 확인한다.
데이터는 모두 합성 값이다."""

import dataclasses
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from eval_tasks_support import real_candidates, real_task_world
from orbit.evaldata.schema import (
    AbstainAnswer,
    ClarifyAnswer,
    DocumentAnswer,
    DraftSentChange,
    EventAnswer,
    EventCancelledChange,
    EventCreatedChange,
    EventSetAnswer,
    EventTimeAnswer,
    EventUpdatedChange,
    MessageSetAnswer,
    ReminderCreatedChange,
    TimeSlotAnswer,
)
from orbit.evaldata.v1 import task_templates as tt
from orbit.evaldata.v1.facts import norm
from orbit.evaldata.v1.skeleton import (
    AllDayTruth,
    CancellationTruth,
    DayAgendaTruth,
    DeadlineTruth,
    Fact,
    MailFactTruth,
    MismatchTruth,
    PersonNextTruth,
    ProjectWeekTruth,
    TzEventTruth,
)
from orbit.evaldata.v1.task_gold import (
    BUILDERS,
    Ctx,
    Draft,
    TaskGenError,
    _reminder_options,
    build_day_agenda,
    trimmed_previous,
)
from orbit.evaldata.v1.task_world import TaskWorld
from orbit.evalrunner.scoring import contains


def drafts(scenario_type: str, form: str | None = None) -> list[Draft]:
    return [
        d
        for (sid, f), d in real_candidates().drafts.items()
        if d.scenario.type == scenario_type and (form is None or f == form)
    ]


def acct_of(d: Draft) -> Any:
    tw, _ = real_task_world()
    return tw.account(d.scenario.account_id)


def asof_day(d: Draft) -> date:
    return d.scenario.as_of.astimezone(acct_of(d).tz).date()


# ---------------------------------------------------------------- 날짜·시간대


def test_every_builder_produces_candidates_for_the_real_world() -> None:
    made = {d.scenario.type for d in real_candidates().drafts.values()}
    assert made == set(BUILDERS)


def test_relative_date_phrases_in_questions_match_the_computed_day() -> None:
    checked = 0
    for t in ("day_agenda", "cancellation", "all_day"):
        for d in drafts(t, "single"):
            truth = d.scenario.truth
            if isinstance(truth, DayAgendaTruth):
                day = truth.day
            elif isinstance(truth, AllDayTruth):
                day = truth.intended_date
            else:
                assert isinstance(truth, CancellationTruth)
                day = truth.original.start.astimezone(acct_of(d).tz).date()
            assert d.keep[0] in d.turns[0]
            assert tt.resolve_date_phrase(d.keep[0], asof_day(d)) == day
            checked += 1
    for d in drafts("project_week", "single"):
        truth = d.scenario.truth
        assert isinstance(truth, ProjectWeekTruth)
        assert tt.resolve_week_phrase(d.keep[0], asof_day(d)) == truth.week_start
        checked += 1
    assert checked >= 20
    # 상대 표현과 절대 표현이 모두 쓰인다
    phrases = [d.keep[0] for d in drafts("day_agenda", "single")]
    assert any(re.fullmatch(r"\d+월 \d+일", p) for p in phrases)


def test_questions_have_no_provider_ids() -> None:
    tw, world = real_task_world()
    pattern = re.compile(r"\bev\d{5,}\b|\bmsg-\d|\bdoc-\d|\bcal-u|\bacct-|\bthr-")
    for d in real_candidates().drafts.values():
        for turn in d.turns:
            assert not pattern.search(turn), turn


def test_la_user_answers_use_los_angeles_offsets_and_local_forms() -> None:
    la = [d for d in real_candidates().drafts.values() if d.scenario.account_id == "acct-u6"]
    assert any(d.scenario.type == "tz_event" for d in la)
    tz = ZoneInfo("America/Los_Angeles")
    checked = 0
    for d in la:
        if d.scenario.type not in ("tz_event", "free_slot", "mismatch", "reschedule"):
            continue
        ans = d.gold.expected_answer
        assert isinstance(ans, EventTimeAnswer | TimeSlotAnswer)
        assert ans.start.utcoffset() is not None
        local = ans.start.astimezone(tz)
        want = tt.local_time_forms(ans.start, tz)
        assert any(f in want for f in d.gold.answer_facts[0].any_of)
        assert f"{local.hour:02d}:{local.minute:02d}" in d.gold.answer_facts[0].any_of
        checked += 1
    assert checked >= 3


def test_tz_event_covers_dst_and_foreign_attendee_variants() -> None:
    variants = {d.variant for d in drafts("tz_event")}
    assert variants == {"local", "foreign_attendee"}
    for d in drafts("tz_event"):
        truth = d.scenario.truth
        assert isinstance(truth, TzEventTruth)
        ans = d.gold.expected_answer
        assert isinstance(ans, EventTimeAnswer) and ans.start == truth.span.start


# ---------------------------------------------------------------- 읽기 정답


def test_day_agenda_excludes_cancelled_and_matches_truth() -> None:
    for d in drafts("day_agenda", "single"):
        truth = d.scenario.truth
        assert isinstance(truth, DayAgendaTruth)
        ans = d.gold.expected_answer
        assert isinstance(ans, EventSetAnswer)
        got = {e.event_id for e in ans.events}
        assert got == {r.id for r in truth.events}
        assert not got & {r.id for r in truth.cancelled}


def test_cancellation_answer_leaves_out_the_cancelled_event_and_world_marks_it() -> None:
    for d in drafts("cancellation"):
        truth = d.scenario.truth
        assert isinstance(truth, CancellationTruth)
        ans = d.gold.expected_answer
        assert isinstance(ans, EventSetAnswer)
        assert truth.event.id not in {e.event_id for e in ans.events}
        assert acct_of(d).event_of(truth.event).status == "cancelled"
        assert "취소" in d.turns[0]


def test_all_day_answer_includes_the_all_day_event_in_calendar_time_zone() -> None:
    assert drafts("all_day")
    for d in drafts("all_day"):
        truth = d.scenario.truth
        assert isinstance(truth, AllDayTruth)
        ans = d.gold.expected_answer
        assert isinstance(ans, EventSetAnswer)
        assert truth.event.id in {e.event_id for e in ans.events}
        ev = acct_of(d).event_of(truth.event)
        assert ev.all_day and ev.start.astimezone(acct_of(d).tz).date() == truth.intended_date
        assert "종일" in d.turns[0]


def test_free_slot_answer_is_the_earliest_slot_that_fits() -> None:
    assert drafts("free_slot")
    for d in drafts("free_slot"):
        ans = d.gold.expected_answer
        assert isinstance(ans, TimeSlotAnswer)
        assert ans.start == d.scenario.truth.free_slots[0].start  # type: ignore[union-attr]
        past = d.scenario.truth.day < asof_day(d)  # type: ignore[union-attr]
        assert ("비어 있던" in d.turns[0] or "비었던" in d.turns[0]) == past


def test_person_next_answer_is_the_first_upcoming_meeting() -> None:
    for d in drafts("person_next", "single"):
        truth = d.scenario.truth
        assert isinstance(truth, PersonNextTruth)
        ans = d.gold.expected_answer
        assert isinstance(ans, EventAnswer)
        assert ans.event.event_id == truth.next_event.id
        assert truth.person_name in d.turns[0]


def test_mail_fact_answer_is_the_latest_message_with_required_facts() -> None:
    assert len(drafts("mail_fact", "single")) >= 40
    for d in drafts("mail_fact", "single"):
        truth = d.scenario.truth
        assert isinstance(truth, MailFactTruth)
        ans = d.gold.expected_answer
        assert isinstance(ans, MessageSetAnswer)
        assert [m.message_id for m in ans.messages] == [truth.latest_message.id]
        assert d.gold.answer_facts_required is True
        assert d.gold.answer_facts[0].any_of == truth.latest.surface_forms
        # 이전 값은 응답에 없어야 한다. 최신 값 표기와 겹치는 표기만 뺀다
        assert set(d.gold.must_not_contain) <= set(truth.previous.surface_forms)
        for form in d.gold.must_not_contain:
            assert not any(contains(latest, form) for latest in truth.latest.surface_forms)


def test_must_not_contain_drops_previous_forms_that_hide_inside_latest_forms() -> None:
    latest = Fact(fact_id="l", kind="amount", value=13_000, surface_forms=["13,000원"])
    previous = Fact(fact_id="p", kind="amount", value=3_000, surface_forms=["3,000원", "3천원"])
    kept, dropped = trimmed_previous(previous, latest)
    assert kept == ["3천원"] and dropped == 1
    other = Fact(fact_id="p", kind="amount", value=1, surface_forms=["9,000원"])
    assert trimmed_previous(other, latest) == (["9,000원"], 0)


def test_mismatch_answer_follows_the_calendar_and_forbids_the_mail_value() -> None:
    assert len(drafts("mismatch")) >= 16
    for d in drafts("mismatch"):
        truth = d.scenario.truth
        assert isinstance(truth, MismatchTruth)
        ans = d.gold.expected_answer
        assert isinstance(ans, EventTimeAnswer)
        assert ans.start == truth.calendar_span.start != truth.mail_start
        assert "time" in truth.fields and d.gold.must_not_contain
        tz = acct_of(d).tz
        cal_forms = tt.local_time_forms(ans.start, tz)
        for forbidden in d.gold.must_not_contain:
            assert forbidden not in cal_forms  # 정답 표기를 금지하지 않는다
            assert not any(contains(f, forbidden) for f in cal_forms)
        mail_forms = tt.local_time_forms(truth.mail_start, tz)
        date_differs = truth.mail_start.astimezone(tz).date() != ans.start.astimezone(tz).date()
        # 메일 값이 시각만 다르면 시각 표기를, 날짜도 다르면 날짜 표기를 금지한다
        assert any(f in d.gold.must_not_contain for f in mail_forms) or date_differs
        if date_differs:
            mail_day = truth.mail_start.astimezone(tz).date()
            assert any(f in d.gold.must_not_contain for f in tt.local_date_forms(mail_day))


def test_clarify_readings_are_distinct_and_exist_in_the_world() -> None:
    assert len(drafts("ambiguous")) >= 16
    for d in drafts("ambiguous"):
        ans = d.gold.expected_answer
        assert isinstance(ans, ClarifyAnswer)
        assert len(ans.readings) == 2
        sigs = [r.model_dump_json() for r in ans.readings]
        assert sigs[0] != sigs[1]
        acct = acct_of(d)
        for r in ans.readings:
            if isinstance(r, EventAnswer):
                assert acct.event(r.event.calendar_id, r.event.event_id) is not None
            elif isinstance(r, EventSetAnswer):
                assert all(acct.event(e.calendar_id, e.event_id) for e in r.events)
            else:
                assert isinstance(r, DocumentAnswer) and r.document.file_id in acct.files


def test_abstain_targets_do_not_exist_in_the_world() -> None:
    tw, world = real_task_world()
    assert {d.variant for d in drafts("absent")} == {
        "person_no_meeting",
        "no_document",
        "no_amount",
        "project_no_meeting",
    }
    for d in drafts("absent"):
        assert isinstance(d.gold.expected_answer, AbstainAnswer)
        raw = world["accounts"][d.scenario.account_id]
        query = d.keep[0]
        q = norm(query)
        if d.variant == "no_document":
            assert not any(q in norm(f["name"]) for f in raw["files"])
        elif d.variant == "project_no_meeting":
            assert not any(q in norm(e["title"]) for e in raw["events"])
        elif d.variant == "person_no_meeting":
            email = d.scenario.truth.person_email  # type: ignore[union-attr]
            assert email is not None and email not in json.dumps(raw["events"])
        else:
            for m in raw["messages"]:
                text = f"{m['subject']} {m['body_text']}"
                assert not (q in norm(text) and re.search(r"\d[\d,]*\s*(?:만\s*)?원", text))


# ---------------------------------------------------------------- 쓰기·리마인더


def test_write_expected_changes_have_the_right_shape() -> None:
    kinds: set[str] = set()
    for d in drafts("meeting_request"):
        (c,) = d.gold.expected_changes  # type: ignore[misc]
        assert isinstance(c, EventCreatedChange)
        assert c.calendar_id == acct_of(d).primary_calendar_id
        assert c.start.utcoffset() is not None and c.end > c.start and c.attendees_include
        kinds.add(c.kind)
    for d in drafts("event_change_target"):
        (c,) = d.gold.expected_changes  # type: ignore[misc]
        if d.variant == "move":
            assert isinstance(c, EventUpdatedChange)
            assert c.fields.start is not None and c.fields.end is not None
            assert c.fields.start != acct_of(d).event(c.calendar_id, c.event_id).start
        else:
            assert isinstance(c, EventCancelledChange)
        kinds.add(c.kind)
    for d in drafts("reply_needed"):
        (c,) = d.gold.expected_changes  # type: ignore[misc]
        assert isinstance(c, DraftSentChange)
        assert c.reply_to_message_id in acct_of(d).messages
        assert acct_of(d).messages[c.reply_to_message_id]["from"]["email"] in c.to_include
        kinds.add(c.kind)
    assert kinds == {"event_created", "event_updated", "event_cancelled", "draft_sent"}


def test_deadline_reminder_time_is_computed_in_the_user_time_zone_and_in_the_future() -> None:
    reqs = drafts("deadline", "request")
    assert reqs
    for d in reqs:
        truth = d.scenario.truth
        assert isinstance(truth, DeadlineTruth)
        (c,) = d.gold.expected_changes  # type: ignore[misc]
        assert isinstance(c, ReminderCreatedChange)
        assert c.due_at > d.scenario.as_of
        tz = acct_of(d).tz
        rule = d.difficulty["reminder_rule"]
        deadline = truth.deadline_at.astimezone(tz)
        if rule == "day_before":
            assert c.due_at.astimezone(tz).date() == deadline.date() - timedelta(days=1)
            assert c.due_at.astimezone(tz).time() == time(9, 0)
        elif rule == "days_before":
            assert c.due_at.astimezone(tz).date() == deadline.date() - timedelta(days=2)
            assert c.due_at.astimezone(tz).time() == time(10, 0)
        else:
            assert c.due_at == deadline - timedelta(hours=3)
        assert c.text_any_of == [truth.item]


def test_reminder_falls_back_to_another_phrasing_when_the_time_is_already_past() -> None:
    reqs = drafts("deadline", "request")
    truth = reqs[0].scenario.truth
    assert isinstance(truth, DeadlineTruth)
    tz = ZoneInfo("Asia/Seoul")
    deadline = datetime(2026, 11, 12, 17, 30, tzinfo=tz)
    as_of = datetime(2026, 11, 11, 10, 30, tzinfo=tz)  # 마감 하루 전 오전 9시는 이미 지났다
    soon = truth.model_copy(update={"deadline_at": deadline, "as_of": as_of})
    options = _reminder_options(soon, tz, as_of)
    assert "day_before" not in options
    assert set(options) == {"hours_before"}  # 이틀 전(11-10)도 과거라 빠진다
    far = truth.model_copy(update={"deadline_at": deadline + timedelta(days=5)})
    assert set(_reminder_options(far, tz, as_of)) == {"day_before", "days_before", "hours_before"}


def test_deadline_question_without_a_request_expects_no_reminder() -> None:
    asks = drafts("deadline", "ask")
    assert asks
    for d in asks:
        assert d.gold.expected_changes == [] and d.gold.expected_answer is None
        assert len(d.gold.answer_facts) == 2  # 날짜와 시각
        assert "알려줘" not in d.turns[0] and "리마인더" not in d.turns[0]


# ---------------------------------------------------------------- 문서


def test_document_tasks_point_at_existing_files_and_require_the_value() -> None:
    for t in ("doc_fact", "doc_version"):
        for d in drafts(t):
            ans = d.gold.expected_answer
            assert isinstance(ans, DocumentAnswer)
            assert ans.document.file_id in acct_of(d).files
            assert d.gold.answer_facts_required is True
    for d in drafts("doc_version"):
        assert d.gold.must_not_contain  # 이전 판의 값
        truth = d.scenario.truth
        latest = acct_of(d).file_of(truth.latest_file)  # type: ignore[union-attr]
        previous = acct_of(d).file_of(truth.previous_file)  # type: ignore[union-attr]
        assert latest["modified_at"] > previous["modified_at"]


# ---------------------------------------------------------------- 다중 턴


def test_multi_turn_gold_is_for_the_last_turn() -> None:
    mt = [d for d in real_candidates().drafts.values() if d.form == "mt"]
    assert mt
    by_type: dict[str, list[Draft]] = {}
    for d in mt:
        by_type.setdefault(d.scenario.type, []).append(d)
    for d in by_type["person_next"]:
        assert 2 <= len(d.turns) <= 3 and d.kind == "multi_turn"
        (c,) = d.gold.expected_changes  # type: ignore[misc]
        assert isinstance(c, EventUpdatedChange)
        ev = acct_of(d).event(c.calendar_id, c.event_id)
        assert ev is not None
        assert c.fields.start == ev.start + timedelta(minutes=30)
        assert c.fields.end == ev.end + timedelta(minutes=30)
        assert "30분" in d.turns[-1]
    for d in by_type["mail_fact"]:
        assert 2 <= len(d.turns) <= 3
        (c,) = d.gold.expected_changes  # type: ignore[misc]
        assert isinstance(c, DraftSentChange) and c.body_facts and c.reply_to_message_id
    for d in by_type["day_agenda"]:
        assert len(d.turns) == 2
        ans = d.gold.expected_answer
        assert isinstance(ans, EventSetAnswer)
        full = [x for x in drafts("day_agenda", "single") if x.scenario is d.scenario]
        assert not full or len(ans.events) < len(full[0].gold.expected_answer.events)  # type: ignore[union-attr]
        tz = acct_of(d).tz
        for e in ans.events:
            ev = acct_of(d).event(e.calendar_id, e.event_id)
            assert ev is not None and ev.start.astimezone(tz).hour >= 12


# ---------------------------------------------------------------- 세계와 어긋나면 생성 실패


def _without_event(tw: TaskWorld, account_id: str, event_id: str) -> TaskWorld:
    acct = tw.accounts[account_id]
    events = tuple(e for e in acct.events if e.id != event_id)
    patched = dataclasses.replace(
        acct, events=events, _by_key={(e.calendar_id, e.id): e for e in events}
    )
    return dataclasses.replace(tw, accounts={**tw.accounts, account_id: patched})


def test_builder_fails_when_the_world_disagrees_with_the_scenario_record() -> None:
    tw, _ = real_task_world()
    sc = next(s for s in tw.skeleton.scenarios if s.type == "day_agenda")
    assert isinstance(build_day_agenda(Ctx(tw, 1), sc, "single"), Draft)
    truth = sc.truth
    assert isinstance(truth, DayAgendaTruth)
    broken = _without_event(tw, sc.account_id, truth.events[0].id)
    with pytest.raises(TaskGenError):
        build_day_agenda(Ctx(broken, 1), sc, "single")


def test_builders_are_deterministic_for_a_seed() -> None:
    tw, _ = real_task_world()
    sc = next(s for s in tw.skeleton.scenarios if s.type == "day_agenda")
    a = build_day_agenda(Ctx(tw, 7), sc, "single")
    b = build_day_agenda(Ctx(tw, 7), sc, "single")
    assert isinstance(a, Draft) and isinstance(b, Draft)
    assert a.turns == b.turns and a.gold == b.gold
