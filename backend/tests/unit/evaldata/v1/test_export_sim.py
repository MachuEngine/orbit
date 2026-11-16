"""export한 세계가 SimWorld에 로드되고 Sim 커넥터 조회로 근거 아티팩트를 찾을 수 있다."""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orbit.connectors.contracts import FileQuery
from orbit.connectors.sim.calendar import SimCalendarConnector
from orbit.connectors.sim.drive import SimDriveConnector
from orbit.connectors.sim.mail import SimMailConnector
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.v1.export import export_world, snippet_of
from orbit.evaldata.v1.skeleton import DayAgendaTruth, Skeleton
from orbit.evaldata.v1.worldmodel import WorldModel


@pytest.fixture(scope="module")
def world(small_sk: Skeleton) -> SimWorld:
    return SimWorld.load(export_world(small_sk), FixedClock(small_sk.snapshot_time))


def test_export_loads_and_counts_match(small_sk: Skeleton, world: SimWorld) -> None:
    for acct, a in small_sk.accounts.items():
        state = world.account(acct)
        assert len(state.messages) == len(a.messages)
        assert sum(len(v) for v in state.events.values()) == len(a.events)
        assert len(state.files) == len(a.files)


def test_export_uses_given_body_text_and_snippet(small_sk: Skeleton) -> None:
    msg = small_sk.accounts["acct-u1"].messages[0]
    data = export_world(small_sk, {msg.body_id: "첫 줄\n  둘째 줄 " + "가" * 100})
    out = next(m for m in data["accounts"]["acct-u1"]["messages"] if m["id"] == msg.id)
    assert out["body_text"].startswith("첫 줄")
    assert out["snippet"] == snippet_of(out["body_text"]) and len(out["snippet"]) <= 60


def _samples(sk: Skeleton) -> list[tuple[str, str]]:
    by: dict[str, list[str]] = defaultdict(list)
    for s in sk.scenarios:
        if len(by[s.type]) < 2:
            by[s.type].append(s.scenario_id)
    return [(t, sid) for t, ids in sorted(by.items()) for sid in ids]


async def test_evidence_is_found_through_sim_connectors(
    small_sk: Skeleton, world: SimWorld
) -> None:
    scenarios = {s.scenario_id: s for s in small_sk.scenarios}
    rt = SimRuntime()
    for _type, sid in _samples(small_sk):
        s = scenarios[sid]
        cal = SimCalendarConnector(world, s.account_id, rt)
        mail = SimMailConnector(world, s.account_id, rt)
        drive = SimDriveConnector(world, s.account_id, rt)
        for ref in [*s.evidence, *s.distractors]:
            if ref.kind == "event":
                assert ref.calendar_id is not None
                assert (await cal.get_event(ref.calendar_id, ref.id)).id == ref.id
            elif ref.kind == "message":
                msg = await mail.get_message(ref.id)
                assert msg.body_text == small_sk.bodies[ref.body_id or ""].template_text
                page = await mail.search_messages(f'subject:"{msg.subject}"', max_results=100)
                assert ref.id in {m.id for m in page.messages}
            else:
                f = await drive.get_file(ref.id)
                text = await drive.read_file_text(ref.id, max_chars=100_000)
                assert text.text == small_sk.bodies[ref.body_id or ""].template_text
                found = await drive.search_files(FileQuery(text=f.name), max_results=50)
                assert ref.id in {x.id for x in found.files}


async def test_day_agenda_matches_list_events(small_sk: Skeleton, world: SimWorld) -> None:
    rt = SimRuntime()
    checked = 0
    for s in small_sk.scenarios:
        t = s.truth
        if not isinstance(t, DayAgendaTruth):
            continue
        cal = SimCalendarConnector(world, s.account_id, rt)
        tz = ZoneInfo(t.time_zone)
        start = datetime(t.day.year, t.day.month, t.day.day, tzinfo=tz)
        got: list[str] = []
        for c in await cal.list_calendars():
            evs = await cal.list_events(c.id, start, start + timedelta(days=1))
            got += [e.id for e in evs if e.status == "confirmed"]
        assert sorted(got) == sorted(r.id for r in t.events)
        checked += 1
    assert checked >= 2


async def test_mail_search_by_sender(
    small_sk: Skeleton, world: SimWorld, small_wm: WorldModel
) -> None:
    u = small_wm.e2e_users()[0]
    mail = SimMailConnector(world, u.account_id, SimRuntime())
    page = await mail.search_messages("in:inbox", max_results=5)
    assert page.messages
