"""Sim 문서 커넥터와 스냅샷 `files` 필드 (ADR-0020 §2). 데이터는 합성 값이다."""

import json
from datetime import UTC, datetime

import pytest

from orbit.connectors.contracts import (
    CALENDAR_TOOLS,
    DRIVE_TOOLS,
    MAIL_TOOLS,
    ConnectorError,
    FileQuery,
    NotFound,
    TransientError,
    UnsupportedFile,
)
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.world import SimWorld, SnapshotError
from orbit.core.clock import FixedClock
from orbit.evaldata.gold import FORBIDDEN_TOOLS
from orbit.evalrunner.tools import tool_specs

from .conftest import FIXTURE_PATH

DRIVE_WORLD = FIXTURE_PATH.with_name("drive_world.json")
NOW = FixedClock(datetime(2026, 11, 12, 9, 0, tzinfo=UTC))


def plan(*rules: dict[str, object]) -> FaultPlan:
    return FaultPlan.from_dict({"rules": list(rules)})


def test_existing_snapshot_loads_and_dumps_without_files() -> None:
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    world = SimWorld.load(FIXTURE_PATH, NOW)
    dumped = world.to_dict()
    assert all("files" not in acct for acct in dumped["accounts"].values())
    assert world.account("acct-alice").files == {}
    # 파일이 없는 스냅샷은 덤프가 원본과 같은 키 집합을 유지한다(해시 불변)
    for aid, acct in raw["accounts"].items():
        assert set(dumped["accounts"][aid]) <= set(acct) | {"draft_log"}


def test_snapshot_with_files_round_trips_and_forks_independently() -> None:
    world = SimWorld.load(DRIVE_WORLD, NOW)
    again = SimWorld.load(world.to_dict(), NOW)
    assert again.to_dict() == world.to_dict()
    assert [f["id"] for f in world.to_dict()["accounts"]["acct-bob"]["files"]] == ["f-bob-1"]
    fork = world.fork(1)
    fork.account("acct-alice").files.pop("f-doc-1")
    assert "f-doc-1" in world.account("acct-alice").files


def test_snapshot_rejects_duplicate_file_ids_and_unknown_fields() -> None:
    data = json.loads(DRIVE_WORLD.read_text(encoding="utf-8"))
    data["accounts"]["acct-alice"]["files"].append(data["accounts"]["acct-alice"]["files"][0])
    with pytest.raises(SnapshotError, match="파일 ID 중복"):
        SimWorld.load(data, NOW)
    data = json.loads(DRIVE_WORLD.read_text(encoding="utf-8"))
    data["accounts"]["acct-alice"]["files"][0]["web_view_link"] = "https://example.com"
    with pytest.raises(SnapshotError):
        SimWorld.load(data, NOW)


def test_drive_methods_are_not_in_the_evaluation_tool_surface() -> None:
    assert {"search_files", "get_file", "read_file_text"} == DRIVE_TOOLS
    assert not DRIVE_TOOLS & (CALENDAR_TOOLS | MAIL_TOOLS)
    assert not DRIVE_TOOLS & set(FORBIDDEN_TOOLS)
    assert not DRIVE_TOOLS & {s.name for s in tool_specs()}


async def test_fault_injection_covers_drive_methods_and_kinds() -> None:
    world = SimWorld.load(DRIVE_WORLD, NOW)
    conns = build_sim_connectors(
        world,
        ["acct-alice"],
        plan(
            {"tool": "search_files", "error": "scope_missing", "nth_call": 1},
            {"tool": "get_file", "error": "transient"},
            {"tool": "read_file_text", "error": "unsupported_file"},
        ),
    )
    drive = conns.drive["acct-alice"]
    with pytest.raises(ConnectorError) as caught:
        await drive.search_files(FileQuery())
    assert caught.value.reason == "scope_missing"
    assert (await drive.search_files(FileQuery())).files  # nth_call=1만 실패한다
    with pytest.raises(TransientError):
        await drive.get_file("f-doc-1")
    with pytest.raises(UnsupportedFile):
        await drive.read_file_text("f-doc-1", max_chars=10)
    assert [(r.tool, r.outcome) for r in conns.calls.records] == [
        ("search_files", "error"),
        ("search_files", "ok"),
        ("get_file", "error"),
        ("read_file_text", "error"),
    ]


def test_fault_rule_for_a_drive_method_binds_without_a_typo() -> None:
    world = SimWorld.load(DRIVE_WORLD, NOW)
    with pytest.raises(ValueError, match="search_file"):
        build_sim_connectors(
            world, ["acct-alice"], plan({"tool": "search_file", "error": "transient"})
        )


async def test_account_id_match_scopes_a_drive_fault_to_one_account() -> None:
    world = SimWorld.load(DRIVE_WORLD, NOW)
    conns = build_sim_connectors(
        world,
        ["acct-alice", "acct-bob"],
        plan({"tool": "get_file", "match": {"account_id": "acct-bob"}, "error": "not_found"}),
    )
    assert (await conns.drive["acct-alice"].get_file("f-doc-1")).id == "f-doc-1"
    with pytest.raises(NotFound):
        await conns.drive["acct-bob"].get_file("f-bob-1")


async def test_call_log_does_not_copy_file_text() -> None:
    world = SimWorld.load(DRIVE_WORLD, NOW)
    conns = build_sim_connectors(world, ["acct-alice"])
    await conns.drive["acct-alice"].read_file_text("f-doc-1", max_chars=1000)
    await conns.drive["acct-alice"].search_files(FileQuery(text="예산"))
    dumped = json.dumps([r.model_dump() for r in conns.calls.records], ensure_ascii=False)
    assert "본문입니다" not in dumped


async def test_reading_does_not_change_the_world() -> None:
    world = SimWorld.load(DRIVE_WORLD, NOW)
    before = json.dumps(world.to_dict(), sort_keys=True)
    drive = build_sim_connectors(world, ["acct-alice"]).drive["acct-alice"]
    await drive.search_files(FileQuery(text="예산"))
    await drive.read_file_text("f-doc-1", max_chars=5)
    assert json.dumps(world.to_dict(), sort_keys=True) == before


def test_connector_requires_an_existing_account() -> None:
    world = SimWorld.load(DRIVE_WORLD, NOW)
    with pytest.raises(KeyError):
        build_sim_connectors(world, ["acct-none"])


async def test_text_search_ranks_name_matches_before_body_matches() -> None:
    world = SimWorld.load(DRIVE_WORLD, NOW)
    drive = build_sim_connectors(world, ["acct-alice"]).drive["acct-alice"]
    page = await drive.search_files(FileQuery(text="예산"))
    assert [f.id for f in page.files] == ["f-sheet-1", "f-doc-1"]  # 이름 일치(예산표)가 먼저
