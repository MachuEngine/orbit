"""호출 기록. 모든 계약 호출을 순서대로 남긴다 (D1-3, 궤적 채점용).

인자·결과는 JSON 직렬화 가능한 요약으로만 남긴다. 본문 전체를 복제하지 않고
ID·필드명 위주로 둔다(ADR-0006 취지).
"""

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

_REDACTED_KEYS = frozenset({"body_text", "description", "snippet"})
_MAX_STR = 80
_ID_FIELDS = (
    "id",
    "calendar_id",
    "thread_id",
    "message_id",
    "draft_id",
    "event_id",
    "version",
    "status",
    "next_page_token",
)


class CallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    tool: str
    args: dict[str, Any]
    outcome: Literal["ok", "error"]
    error_type: str | None = None
    result_summary: Any = None


def summarize_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """인자를 JSON 직렬화 가능한 요약으로 바꾼다. 본문 필드는 길이만 남긴다."""
    return {key: _summarize_value(key, value) for key, value in args.items()}


def _summarize_value(key: str, value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    if isinstance(value, str):
        if key in _REDACTED_KEYS or len(value) > _MAX_STR:
            return f"<str len={len(value)}>"
        return value
    if isinstance(value, Mapping):
        return {str(k): _summarize_value(str(k), v) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [_summarize_value(key, v) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)


def _ids_of(item: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name in _ID_FIELDS:
        attr = getattr(item, name, None)
        if isinstance(attr, str):
            found[name] = attr
    return found


def summarize_result(value: Any) -> Any:
    """결과를 ID·개수 위주 요약으로 바꾼다."""
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return {"type": "list", "count": len(value), "items": [_ids_of(v) for v in value]}
    if isinstance(value, BaseModel):
        summary: dict[str, Any] = {"type": type(value).__name__, **_ids_of(value)}
        items = getattr(value, "messages", None)
        if isinstance(items, tuple):
            summary["count"] = len(items)
            summary["items"] = [_ids_of(m) for m in items]
        return summary
    return _summarize_value("result", value)


class CallLog:
    """호출 기록 목록. `seq`는 1부터 호출 순서대로 붙는다."""

    def __init__(self) -> None:
        self._records: list[CallRecord] = []

    @property
    def records(self) -> list[CallRecord]:
        return list(self._records)

    def record(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        outcome: Literal["ok", "error"],
        error_type: str | None = None,
        result: Any = None,
    ) -> CallRecord:
        rec = CallRecord(
            seq=len(self._records) + 1,
            tool=tool,
            args=summarize_args(args),
            outcome=outcome,
            error_type=error_type,
            result_summary=summarize_result(result),
        )
        self._records.append(rec)
        return rec
