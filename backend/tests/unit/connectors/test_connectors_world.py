import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orbit.connectors.contracts import CalendarEvent, Draft, DraftSpec, MailMessage
from orbit.connectors.sim.world import (
    IdGenerator,
    Normalization,
    SimWorld,
    SnapshotError,
    diff,
)
from orbit.core.clock import FixedClock

from .conftest import FIXTURE_PATH


def raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return data


def test_load_from_path_and_dict(fixed_clock: FixedClock) -> None:
    by_path = SimWorld.load(FIXTURE_PATH, fixed_clock)
    by_dict = SimWorld.load(raw(), fixed_clock)
    assert by_path.to_dict() == by_dict.to_dict()
    assert by_path.account_ids == ["acct-alice", "acct-bob"]
    alice = by_path.account("acct-alice")
    assert alice.email == "alice@example.com"
    assert len(alice.events["cal-alice-main"]) == 2
    assert alice.messages["msg-a-003"].has_attachment
    assert "drf-a-001" in alice.drafts


def test_to_dict_round_trips(world: SimWorld, fixed_clock: FixedClock) -> None:
    again = SimWorld.load(world.to_dict(), fixed_clock)
    assert again.to_dict() == world.to_dict()


def test_unknown_account(world: SimWorld) -> None:
    with pytest.raises(KeyError):
        world.account("nope")


def _mutate_version(d: dict[str, Any]) -> None:
    d["schema_version"] = 2


def _naive_time(d: dict[str, Any]) -> None:
    d["accounts"]["acct-alice"]["events"][0]["start"] = "2026-11-12T01:00:00"


def _missing_calendar(d: dict[str, Any]) -> None:
    d["accounts"]["acct-alice"]["events"][0]["calendar_id"] = "cal-none"


def _message_not_in_thread(d: dict[str, Any]) -> None:
    d["accounts"]["acct-alice"]["threads"][0]["message_ids"] = ["msg-a-001"]


def _draft_without_spec(d: dict[str, Any]) -> None:
    del d["accounts"]["acct-alice"]["drafts"][0]["spec"]


def _duplicate_event(d: dict[str, Any]) -> None:
    events = d["accounts"]["acct-alice"]["events"]
    events.append(copy.deepcopy(events[0]))


def _extra_key(d: dict[str, Any]) -> None:
    d["accounts"]["acct-bob"]["unknown"] = 1


def _bad_seed(d: dict[str, Any]) -> None:
    d["seed"] = "x"


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_version,
        _naive_time,
        _missing_calendar,
        _message_not_in_thread,
        _draft_without_spec,
        _duplicate_event,
        _extra_key,
        _bad_seed,
    ],
)
def test_invalid_snapshot_raises_snapshot_error(
    mutate: Callable[[dict[str, Any]], None], fixed_clock: FixedClock
) -> None:
    data = raw()
    mutate(data)
    with pytest.raises(SnapshotError):
        SimWorld.load(data, fixed_clock)


def test_load_missing_file(fixed_clock: FixedClock, tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        SimWorld.load(tmp_path / "none.json", fixed_clock)


def test_fork_is_independent(world: SimWorld) -> None:
    fork = world.fork(42)
    fork.account("acct-alice").events["cal-alice-main"].pop("evalice001")
    fork.account("acct-alice").drafts.clear()
    assert "evalice001" in world.account("acct-alice").events["cal-alice-main"]
    assert "drf-a-001" in world.account("acct-alice").drafts
    world.account("acct-bob").messages.clear()
    assert fork.account("acct-bob").messages


def test_deterministic_ids() -> None:
    def sequence(seed: int) -> list[str]:
        g = IdGenerator(seed)
        return [g.message_id(), g.draft_id(), g.version(), g.thread_id(), g.message_id()]

    assert sequence(1) == sequence(1)
    assert sequence(1) != sequence(2)
    assert len(set(sequence(1))) == 5


def test_fork_resets_id_generator_by_episode_seed(world: SimWorld) -> None:
    assert world.fork(5).ids.message_id() == world.fork(5).ids.message_id()
    assert world.fork(5).ids.message_id() != world.fork(6).ids.message_id()


def _event(world: SimWorld, **overrides: Any) -> CalendarEvent:
    base = world.account("acct-alice").events["cal-alice-main"]["evalice001"]
    return base.model_copy(update=overrides)


def test_diff_identical_is_empty(world: SimWorld) -> None:
    assert diff(world, world.copy()).is_empty


def test_diff_event_created_updated_cancelled(world: SimWorld) -> None:
    before = world.copy()
    alice = world.account("acct-alice")
    now = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    new = CalendarEvent(
        id="evnew00001",
        calendar_id="cal-alice-main",
        title="신규",
        start=now,
        end=now,
        version=world.ids.version(),
        updated_at=now,
    )
    alice.events["cal-alice-main"]["evnew00001"] = new
    alice.events["cal-alice-main"]["evalice001"] = _event(
        world, title="바뀐 제목", version="v-new", updated_at=now
    )
    alice.events["cal-alice-main"]["evalice002"] = alice.events["cal-alice-main"][
        "evalice002"
    ].model_copy(update={"status": "cancelled", "version": "v-c", "updated_at": now})

    d = diff(before, world)
    assert [e.event["id"] for e in d.events_created] == ["evnew00001"]
    assert "version" not in d.events_created[0].event
    assert len(d.events_updated) == 1
    changes = d.events_updated[0].changes
    assert d.events_updated[0].event_id == "evalice001"
    assert set(changes) == {"title"}
    assert changes["title"].before == "주간 회의"
    assert changes["title"].after == "바뀐 제목"
    assert [c.event_id for c in d.events_cancelled] == ["evalice002"]

    raw_diff = diff(before, world, normalize=Normalization(ignore_volatile_fields=False))
    assert {"title", "version", "updated_at"} == set(raw_diff.events_updated[0].changes)
    assert "version" in raw_diff.events_created[0].event


def test_diff_volatile_only_change_is_empty(world: SimWorld) -> None:
    before = world.copy()
    alice = world.account("acct-alice")
    alice.events["cal-alice-main"]["evalice001"] = _event(
        world, version="other", updated_at=datetime(2026, 11, 20, tzinfo=UTC)
    )
    assert diff(before, world).is_empty
    assert not diff(before, world, normalize=Normalization(ignore_volatile_fields=False)).is_empty


def _new_draft(world: SimWorld, thread_id: str) -> Draft:
    spec = DraftSpec.model_validate(
        {"to": [{"email": "bob@example.org"}], "subject": "제목", "body_text": "본문"}
    )
    return Draft(
        draft_id=world.ids.draft_id(),
        message_id=world.ids.message_id(),
        thread_id=thread_id,
        spec=spec,
    )


def test_diff_draft_created_deleted(world: SimWorld) -> None:
    before = world.copy()
    alice = world.account("acct-alice")
    del alice.drafts["drf-a-001"]
    draft = _new_draft(world, "thr-a-002")
    alice.drafts[draft.draft_id] = draft

    d = diff(before, world)
    assert len(d.drafts_created) == 1
    assert "draft_id" not in d.drafts_created[0].draft
    assert "thread_id" in d.drafts_created[0].draft  # 기존 스레드 ID는 지우지 않는다
    assert len(d.drafts_deleted) == 1
    assert "draft_id" not in d.drafts_deleted[0].draft
    assert not d.drafts_sent

    keep = diff(before, world, normalize=Normalization(ignore_generated_ids=False))
    assert keep.drafts_created[0].draft["draft_id"] == draft.draft_id


def test_diff_draft_sent_and_message_added(world: SimWorld) -> None:
    before = world.copy()
    alice = world.account("acct-alice")
    draft = alice.drafts.pop("drf-a-001")
    sent = MailMessage.model_validate(
        {
            "id": world.ids.message_id(),
            "thread_id": draft.thread_id,
            "from": {"email": "alice@example.com"},
            "to": [{"email": "bob@example.org"}],
            "subject": "Re: 프로젝트 일정 문의",
            "body_text": "초안 합성 본문입니다.",
            "sent_at": "2026-11-11T09:00:00+00:00",
            "labels": ["SENT"],
        }
    )
    alice.messages[sent.id] = sent
    alice.threads[draft.thread_id].append(sent.id)
    other = sent.model_copy(update={"id": world.ids.message_id(), "labels": ("INBOX",)})
    alice.messages[other.id] = other
    alice.threads[draft.thread_id].append(other.id)

    d = diff(before, world)
    assert len(d.drafts_sent) == 1
    assert d.drafts_sent[0].message["labels"] == ["SENT"]
    assert not d.drafts_deleted
    assert len(d.messages_added) == 1
    assert d.messages_added[0].message["labels"] == ["INBOX"]
    assert "id" not in d.messages_added[0].message


def test_fork_and_copy_share_the_same_clock(world: SimWorld, fixed_clock: FixedClock) -> None:
    assert world.fork(7).clock is world.clock
    assert world.copy().clock is world.clock
    fixed_clock.advance(timedelta(minutes=5))
    assert world.fork(7).clock.now() == world.clock.now()
    assert world.copy().clock.now() == world.clock.now()


def test_fork_accepts_own_clock_for_parallel_episodes(
    world: SimWorld, fixed_clock: FixedClock
) -> None:
    own = FixedClock(datetime(2030, 1, 1, tzinfo=UTC))
    fork = world.fork(3, clock=own)
    assert fork.clock is own
    own.advance(timedelta(hours=1))
    assert world.clock.now() == fixed_clock.now()
    assert fork.account("acct-alice").events == world.account("acct-alice").events
