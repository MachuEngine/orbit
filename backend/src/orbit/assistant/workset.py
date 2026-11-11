"""대화 범위 작업 집합 (D8-1). 모델은 `E1`·`M1` 같은 핸들만 보고, 제공자 ID는 서버가 보관한다.

핸들은 처음 본 순서대로 번호가 붙고, 같은 대상은 항상 같은 핸들이다.
"""

import re

_EVENT_HANDLE = re.compile(r"E[1-9][0-9]*")
_MESSAGE_HANDLE = re.compile(r"M[1-9][0-9]*")


class UnknownHandleError(ValueError):
    """작업 집합에 없는 핸들이다. 메시지에 입력 원문을 넣지 않는다."""

    def __init__(self) -> None:
        super().__init__("알 수 없는 핸들")


def _normalize(handle: object) -> str:
    if not isinstance(handle, str):
        raise UnknownHandleError
    return handle.strip().upper()


class WorkSet:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], str] = {}
        self._event_by_handle: dict[str, tuple[str, str]] = {}
        self._messages: dict[str, str] = {}
        self._message_by_handle: dict[str, str] = {}

    def event_handle(self, calendar_id: str, event_id: str) -> str:
        key = (calendar_id, event_id)
        handle = self._events.get(key)
        if handle is None:
            handle = f"E{len(self._events) + 1}"
            self._events[key] = handle
            self._event_by_handle[handle] = key
        return handle

    def message_handle(self, message_id: str) -> str:
        handle = self._messages.get(message_id)
        if handle is None:
            handle = f"M{len(self._messages) + 1}"
            self._messages[message_id] = handle
            self._message_by_handle[handle] = message_id
        return handle

    def resolve_event(self, handle: object) -> tuple[str, str]:
        """핸들을 (calendar_id, event_id)로 바꾼다."""
        h = _normalize(handle)
        if not _EVENT_HANDLE.fullmatch(h) or h not in self._event_by_handle:
            raise UnknownHandleError
        return self._event_by_handle[h]

    def resolve_message(self, handle: object) -> str:
        h = _normalize(handle)
        if not _MESSAGE_HANDLE.fullmatch(h) or h not in self._message_by_handle:
            raise UnknownHandleError
        return self._message_by_handle[h]
