"""평가 도구 목록은 9단계(색인 동기화 메서드 추가) 이전과 같다 (ADR-0019 §8)."""

from types import SimpleNamespace

from orbit.connectors.contracts import MAIL_SYNC_METHODS, MAIL_TOOLS
from orbit.evaldata.gold import FORBIDDEN_TOOLS
from orbit.evalrunner.tools import ToolArgumentError, ToolDispatcher, tool_specs

PRE_STAGE9_TOOLS = [
    "cancel_event",
    "create_draft",
    "create_event",
    "delete_draft",
    "get_draft",
    "get_event",
    "get_message",
    "get_thread",
    "list_calendars",
    "list_events",
    "search_messages",
    "send_draft",
    "update_event",
]


def test_eval_tool_specs_are_unchanged() -> None:
    assert [s.name for s in tool_specs()] == PRE_STAGE9_TOOLS


def test_sync_methods_are_contract_tools_but_not_eval_or_forbidden() -> None:
    assert MAIL_SYNC_METHODS <= MAIL_TOOLS
    assert not MAIL_SYNC_METHODS & set(FORBIDDEN_TOOLS)
    assert FORBIDDEN_TOOLS == [
        "cancel_event",
        "create_draft",
        "create_event",
        "delete_draft",
        "send_draft",
        "update_event",
    ]


async def test_dispatcher_rejects_sync_methods() -> None:
    dispatcher = ToolDispatcher(SimpleNamespace(mail=None, calendar=None))  # type: ignore[arg-type]
    for name in sorted(MAIL_SYNC_METHODS):
        try:
            await dispatcher.call(name, {})
        except ToolArgumentError:
            continue
        raise AssertionError(name)
