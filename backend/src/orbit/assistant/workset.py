"""대화 범위 작업 집합 (D8-1). 모델은 `E1`·`M1`·`D1`(문서) 같은 핸들만 보고,
제공자 ID는 서버가 보관한다.

핸들은 처음 본 순서대로 번호가 붙고, 같은 대상은 항상 같은 핸들이다.
"""

import re
from collections.abc import Mapping
from typing import Any

_EVENT_HANDLE = re.compile(r"E[1-9][0-9]*")
_MESSAGE_HANDLE = re.compile(r"M[1-9][0-9]*")
_DOCUMENT_HANDLE = re.compile(r"D[1-9][0-9]*")


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
        self._documents: dict[str, str] = {}
        self._document_by_handle: dict[str, str] = {}

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

    def document_handle(self, file_id: str) -> str:
        handle = self._documents.get(file_id)
        if handle is None:
            handle = f"D{len(self._documents) + 1}"
            self._documents[file_id] = handle
            self._document_by_handle[handle] = file_id
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

    def resolve_document(self, handle: object) -> str:
        h = _normalize(handle)
        if not _DOCUMENT_HANDLE.fullmatch(h) or h not in self._document_by_handle:
            raise UnknownHandleError
        return self._document_by_handle[h]

    def export(self) -> dict[str, Any]:
        """핸들 -> 제공자 ID 매핑을 JSON dict로 내보낸다(서버 저장용이며 모델에 주지 않는다)."""
        data: dict[str, Any] = {
            "events": {h: list(key) for h, key in self._event_by_handle.items()},
            "messages": dict(self._message_by_handle),
        }
        if self._document_by_handle:  # 문서가 없으면 기존 저장 형식과 같다(하위 호환)
            data["documents"] = dict(self._document_by_handle)
        return data

    @classmethod
    def restore(cls, data: Mapping[str, Any]) -> "WorkSet":
        """`export` 결과로 작업 집합을 복원한다. 번호가 1부터 이어지지 않으면 `ValueError`."""
        workset = cls()
        for handle, ref in sorted(data.get("events", {}).items(), key=lambda kv: int(kv[0][1:])):
            calendar_id, event_id = ref
            if workset.event_handle(calendar_id, event_id) != handle:
                raise ValueError("작업 집합 핸들 번호가 이어지지 않는다")
        for handle, message_id in sorted(
            data.get("messages", {}).items(), key=lambda kv: int(kv[0][1:])
        ):
            if workset.message_handle(message_id) != handle:
                raise ValueError("작업 집합 핸들 번호가 이어지지 않는다")
        for handle, file_id in sorted(
            data.get("documents", {}).items(), key=lambda kv: int(kv[0][1:])
        ):
            if workset.document_handle(file_id) != handle:
                raise ValueError("작업 집합 핸들 번호가 이어지지 않는다")
        return workset
