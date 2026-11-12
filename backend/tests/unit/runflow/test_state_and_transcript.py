"""그래프 상태 검사, transcript 복원, 쓰기 도구 해석 (ADR-0006, 0011, 0012 §4). DB 없이 실행."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from orbit.actions.models import CancelEvent, CreateEvent, SendMail, UpdateEventTime
from orbit.api.app import create_app
from orbit.assistant.tools import ToolInputError
from orbit.assistant.workset import WorkSet
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.core.config import Settings
from orbit.records.runs import RunMessageRecord
from orbit.runflow.nodes import orbit_tool_call_id
from orbit.runflow.state import MAX_STATE_STRING, StateViolation, validate_state
from orbit.runflow.summary import approval_summary
from orbit.runflow.transcript import Transcript
from orbit.runflow.write_tools import (
    LOCAL_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    build_action,
    recipient_emails,
    run_tool_specs,
)

WORLD = Path(__file__).resolve().parents[2] / "fixtures" / "sim" / "minimal_world.json"
NOW = datetime(2026, 11, 11, tzinfo=UTC)


# ---------------------------------------------------------------- 그래프 상태


def test_state_accepts_ids_step_and_control_values() -> None:
    state = {
        "run_id": str(uuid.uuid4()),
        "workspace_id": str(uuid.uuid4()),
        "step": 3,
        "tool_call_id": "",
        "next_node": "await_approval",
    }
    assert validate_state(state) == state


@pytest.mark.parametrize(
    "bad",
    [
        {"body": "합성 본문"},  # 허용되지 않은 키
        {"run_id": "x" * (MAX_STATE_STRING + 1)},  # 길이 상한
        {"run_id": "메일 본문 같은 긴 문자열"},  # UUID가 아니다
        {"run_id": "bob@example.org"},  # 주소
        {"step": -1},
        {"step": True},
        {"step": 10_000},
        {"step": "1"},
        {"next_node": "somewhere"},
        {"tool_call_id": 5},
    ],
)
def test_state_rejects_non_control_values(bad: dict[str, Any]) -> None:
    with pytest.raises(StateViolation):
        validate_state(bad)


def test_state_violation_message_has_no_values() -> None:
    with pytest.raises(StateViolation) as info:
        validate_state({"run_id": "bob-secret@example.org"})
    assert "bob-secret" not in str(info.value)


def test_tool_call_id_is_deterministic_per_run_step_and_index() -> None:
    run = uuid.uuid4()
    assert orbit_tool_call_id(run, 1, 0) == orbit_tool_call_id(run, 1, 0)
    ids = {orbit_tool_call_id(run, s, i) for s in range(3) for i in range(3)}
    assert len(ids) == 9
    assert orbit_tool_call_id(uuid.uuid4(), 1, 0) != orbit_tool_call_id(run, 1, 0)


# ---------------------------------------------------------------- 작업 집합 복원


def test_workset_export_restore_keeps_handles() -> None:
    ws = WorkSet()
    assert ws.event_handle("cal-1", "ev-a") == "E1"
    assert ws.message_handle("msg-x") == "M1"
    assert ws.event_handle("cal-1", "ev-b") == "E2"
    restored = WorkSet.restore(ws.export())
    assert restored.resolve_event("E2") == ("cal-1", "ev-b")
    assert restored.resolve_message("M1") == "msg-x"
    assert restored.event_handle("cal-1", "ev-a") == "E1"  # 같은 대상은 같은 핸들
    assert restored.event_handle("cal-1", "ev-c") == "E3"  # 이어서 번호를 붙인다


def test_workset_restore_rejects_gaps() -> None:
    with pytest.raises(ValueError, match="핸들"):
        WorkSet.restore({"events": {"E2": ["cal", "ev"]}})


# ---------------------------------------------------------------- transcript


def _row(seq: int, role: str, content: dict[str, Any]) -> RunMessageRecord:
    return RunMessageRecord(seq=seq, role=role, content=content, created_at=NOW)


def _transcript() -> Transcript:
    list_result = (
        '{"events": [{"handle": "E1", "title": "회의", "attendees": '
        '[{"name": "밥", "email": "bob@example.org"}], '
        '"organizer": {"name": "앨리스", "email": "alice@example.com"}}]}'
    )
    mail_result = (
        '{"handle": "M1", "subject": "문의", "from": {"name": "밥", "email": "bob@example.org"},'
        ' "to": [{"email": "alice@example.com", "name": null}], "cc": [],'
        ' "body": "연락처 carol@example.net"}'
    )
    return Transcript(
        (
            _row(1, "user", {"text": "dave@example.org 에게 메일 보내줘"}),
            _row(
                2,
                "assistant",
                {
                    "step": 0,
                    "text": None,
                    "tool_calls": [{"id": "call-1", "name": "list_events", "arguments": {}}],
                },
            ),
            _row(
                3,
                "tool",
                {
                    "step": 0,
                    "tool_call_id": "call-1",
                    "name": "list_events",
                    "text": list_result,
                    "is_error": False,
                    "handles": {"events": {"E1": ["cal", "ev"]}, "messages": {}},
                },
            ),
            _row(
                4,
                "tool",
                {
                    "step": 1,
                    "tool_call_id": "call-1",  # 호출 ID는 step마다 겹칠 수 있다
                    "name": "read_mail",
                    "text": mail_result,
                    "is_error": False,
                    "handles": {
                        "events": {"E1": ["cal", "ev"]},
                        "messages": {"M1": "msg-1"},
                    },
                },
            ),
            _row(
                5,
                "tool",
                {
                    "step": 1,
                    "tool_call_id": "call-9",
                    "name": "x",
                    "text": '{"error": "invalid_arguments"}',
                    "is_error": True,
                },
            ),
        )
    )


def test_transcript_restores_workset_from_latest_tool_row() -> None:
    workset = _transcript().workset()
    assert workset.resolve_event("E1") == ("cal", "ev")
    assert workset.resolve_message("M1") == "msg-1"


def test_transcript_finds_results_by_step_and_call_id() -> None:
    t = _transcript()
    assert set(t.tool_results(0)) == {"call-1"}
    assert set(t.tool_results(1)) == {"call-1", "call-9"}
    assert t.assistant_for_step(0) is not None
    assert t.assistant_for_step(1) is None


def test_transcript_provenance_inputs_split_structural_emails_and_external_text() -> None:
    emails, texts = _transcript().provenance_inputs()
    assert sorted(emails) == [
        "alice@example.com",
        "alice@example.com",
        "bob@example.org",
        "bob@example.org",
    ]
    assert any("carol@example.net" in t for t in texts)  # 본문의 주소는 구조 필드가 아니다
    assert "carol@example.net" not in emails


def test_transcript_builds_llm_messages_in_order() -> None:
    messages = _transcript().llm_messages("시스템", "0123456789ab")
    assert [m.role for m in messages] == ["system", "user", "assistant", "tool", "tool", "tool"]
    assert messages[2].tool_calls[0].name == "list_events"
    assert messages[3].tool_call_id == "call-1"
    for m in messages[3:]:  # 도구 결과는 모두 봉투 안(ADR-0018)
        assert (m.content or "").startswith('<orbit_data source="')
        assert (m.content or "").endswith('</orbit_data boundary="0123456789ab">')


# ---------------------------------------------------------------- 도구 정의와 인자 해석


def test_model_gets_read_context_and_write_tools_without_submit_answer() -> None:
    names = [spec.name for spec in run_tool_specs()]
    assert sorted(names) == sorted(READ_TOOLS | WRITE_TOOLS | LOCAL_TOOLS)
    assert len(names) == 13 and "conversation_context" in READ_TOOLS  # 문서 2개, 리마인더 1개 포함
    assert {"create_reminder"} == LOCAL_TOOLS and not LOCAL_TOOLS & (READ_TOOLS | WRITE_TOOLS)
    assert "search_indexed_mail" in READ_TOOLS
    assert "submit_answer" not in names


async def _env() -> tuple[Any, Any, WorkSet]:
    world = SimWorld.load(WORLD, FixedClock(NOW))
    conns = build_sim_connectors(world, ["acct-alice"])
    workset = WorkSet()
    workset.event_handle("cal-alice-main", "evalice002")
    workset.message_handle("msg-a-001")
    return conns.calendar["acct-alice"], conns.mail["acct-alice"], workset


async def test_build_action_resolves_handles_to_provider_ids() -> None:
    calendar, mail, workset = await _env()

    async def build(name: str, args: object) -> Any:
        return await build_action(name, args, workset=workset, calendar=calendar, mail=mail)

    update = await build(
        "update_event_time",
        {"handle": "e1", "start": "2026-11-12T14:00:00+09:00", "end": "2026-11-12T15:00:00+09:00"},
    )
    assert isinstance(update, UpdateEventTime)
    assert (update.calendar_id, update.event_id) == ("cal-alice-main", "evalice002")
    cancel = await build("cancel_event", {"handle": "E1"})
    assert isinstance(cancel, CancelEvent)
    create = await build(
        "create_event",
        {
            "title": "합성",
            "start": "2026-11-14T10:00:00+09:00",
            "end": "2026-11-14T11:00:00+09:00",
            "attendees": ["bob@example.org"],
        },
    )
    assert isinstance(create, CreateEvent)
    assert create.calendar_id == "cal-alice-main"  # 기본(primary) 캘린더
    assert recipient_emails(create) == ["bob@example.org"]
    mail_action = await build(
        "send_mail",
        {"to": ["bob@example.org"], "cc": ["x@example.org"], "subject": "s", "body": "b",
         "reply_to_handle": "M1"},
    )  # fmt: skip
    assert isinstance(mail_action, SendMail)
    assert mail_action.reply_to_thread_id == "thr-a-001"
    assert recipient_emails(mail_action) == ["bob@example.org", "x@example.org"]


@pytest.mark.parametrize(
    ("name", "args", "kind"),
    [
        ("cancel_event", {"handle": "E9"}, "unknown_handle"),
        ("cancel_event", {"handle": "E1", "extra": 1}, "invalid_arguments"),
        ("cancel_event", "E1", "invalid_arguments"),
        ("update_event_time", {"handle": "E1", "start": "2026-11-12T14:00:00", "end": "x"},
         "invalid_arguments"),
        ("create_event", {"title": "t", "start": "2026-11-12T14:00:00+09:00",
                          "end": "2026-11-12T13:00:00+09:00"}, "invalid_arguments"),
        ("create_event", {"title": "t", "start": "2026-11-12T14:00:00+09:00",
                          "end": "2026-11-12T15:00:00+09:00", "attendees": ["bad address"]},
         "invalid_arguments"),
        ("send_mail", {"to": [], "subject": "s", "body": "b"}, "invalid_arguments"),
        ("send_mail", {"to": ["a@example.org"], "subject": "s", "body": "b",
                       "reply_to_handle": "M9"}, "unknown_handle"),
        ("delete_everything", {}, "unknown_tool"),
    ],
)  # fmt: skip
async def test_build_action_rejects_bad_arguments(name: str, args: object, kind: str) -> None:
    calendar, mail, workset = await _env()
    with pytest.raises(ToolInputError) as info:
        await build_action(name, args, workset=workset, calendar=calendar, mail=mail)
    assert info.value.kind == kind


def test_approval_summary_has_no_body_or_addresses() -> None:
    args = {
        "to": ["bob@example.org"],
        "cc": [],
        "subject": "비밀 제목",
        "body": "비밀 본문",
        "reply_to_thread_id": "thr-1",
    }
    summary = approval_summary("send_mail", args, {})
    assert summary == {"to_count": 1, "cc_count": 0, "is_reply": True}
    assert "bob" not in str(summary) and "비밀" not in str(summary)


# ---------------------------------------------------------------- 임시 인증


def test_app_refuses_to_start_outside_dev_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORBIT_ENV", raising=False)
    with pytest.raises(RuntimeError, match="ORBIT_ENV=dev"):
        create_app(Settings(database_url=SecretStr("postgresql+psycopg://u:p@h/d")))
    with pytest.raises(RuntimeError, match="ORBIT_ENV=dev"):
        create_app(Settings(database_url=SecretStr("postgresql+psycopg://u:p@h/d"), env="prod"))
    assert create_app(Settings(database_url=SecretStr("postgresql+psycopg://u:p@h/d"), env="dev"))
