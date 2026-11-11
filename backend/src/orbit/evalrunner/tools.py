"""계약 도구 호출 변환기: 모델이 낸 JSON 인자를 Connector 메서드 호출로 바꾼다.

Connector Protocol의 시그니처에서 인자 타입을 읽어 pydantic으로 검증·변환하므로 도구마다 코드를
따로 쓰지 않는다. 기준 SUT(`ScriptedToolSUT`)와 이후 Tool Calling 루프의 골격으로 쓴다.
모델은 제공자 ID가 아닌 인자를 그대로 넘기는 v0 단계다(작업 집합 핸들 변환은 3d에서 붙인다).
"""

import inspect
import json
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import to_jsonable_python

from orbit.connectors.contracts import (
    CALENDAR_TOOLS,
    MAIL_TOOLS,
    CalendarConnector,
    MailConnector,
)
from orbit.evalrunner.episode import EpisodeConnectors
from orbit.llm.types import ToolSpec


@dataclass(frozen=True)
class _Param:
    name: str
    adapter: TypeAdapter[Any]
    required: bool


def _build() -> dict[str, tuple[str, tuple[_Param, ...]]]:
    table: dict[str, tuple[str, tuple[_Param, ...]]] = {}
    for kind, proto, names in (
        ("calendar", CalendarConnector, CALENDAR_TOOLS),
        ("mail", MailConnector, MAIL_TOOLS),
    ):
        for name in sorted(names):
            fn = getattr(proto, name)
            hints = typing.get_type_hints(fn)
            params = tuple(
                _Param(p.name, TypeAdapter(hints[p.name]), p.default is inspect.Parameter.empty)
                for p in inspect.signature(fn).parameters.values()
                if p.name != "self"
            )
            table[name] = (kind, params)
    return table


_TABLE = _build()


def tool_specs() -> tuple[ToolSpec, ...]:
    """모델에 노출할 도구 정의. v0은 인자 스키마를 느슨하게(객체) 둔다."""
    return tuple(
        ToolSpec(name=n, description=f"{kind} 도구 {n}", parameters={"type": "object"})
        for n, (kind, _) in sorted(_TABLE.items())
    )


class ToolArgumentError(ValueError):
    """모델이 낸 도구 이름·인자가 계약과 맞지 않는다."""


class ToolDispatcher:
    def __init__(self, connectors: EpisodeConnectors) -> None:
        self._mail = connectors.mail
        self._calendar = connectors.calendar

    async def call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        entry = _TABLE.get(name)
        if entry is None:
            raise ToolArgumentError(f"알 수 없는 도구: {name}")
        kind, params = entry
        unknown = set(arguments) - {p.name for p in params}
        if unknown:
            raise ToolArgumentError(f"{name}: 알 수 없는 인자 {sorted(unknown)}")
        kwargs: dict[str, Any] = {}
        for p in params:
            if p.name in arguments:
                kwargs[p.name] = p.adapter.validate_python(arguments[p.name])
            elif p.required:
                raise ToolArgumentError(f"{name}: 필수 인자 {p.name}이 없다")
        target: Any = self._calendar if kind == "calendar" else self._mail
        return await getattr(target, name)(**kwargs)


def serialize_result(result: Any) -> str:
    """도구 결과를 LLM 메시지용 JSON 문자열로 바꾼다."""
    return json.dumps(to_jsonable_python(result, by_alias=True), ensure_ascii=False, sort_keys=True)
