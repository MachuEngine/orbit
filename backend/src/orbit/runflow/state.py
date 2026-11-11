"""그래프 상태 스키마 (ADR-0006 결정 1·4).

LangGraph 체크포인트는 RLS 밖 테이블이다. 상태에는 식별자와 짧은 제어 값만 둔다.
메일·문서·일정 본문, 주소, 이름, 프롬프트·응답 원문은 넣지 않는다. 대화는 `run_message`에 있다.
모든 노드의 입력·출력은 `validate_state`를 거쳐 허용된 키와 형식, 길이 상한을 확인한다.
"""

import uuid
from collections.abc import Mapping
from typing import Any, TypedDict

MAX_STATE_STRING = 64  # UUID 문자열(36자)과 짧은 제어 값만 들어간다
MAX_STEP = 1000

NEXT_NODES = frozenset({"agent", "await_approval", "execute", "done"})
_UUID_KEYS = frozenset({"run_id", "workspace_id", "tool_call_id"})


class RunState(TypedDict, total=False):
    run_id: str
    workspace_id: str
    step: int
    tool_call_id: str  # 승인 대기 중인 tool_call의 ID. 없으면 빈 문자열
    next_node: str


class StateViolation(ValueError):
    """그래프 상태에 허용되지 않은 값이 들어왔다. 메시지에 값을 넣지 않는다."""


def validate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """허용된 키와 형식인지 확인하고 사본을 돌려준다. 어긋나면 `StateViolation`."""
    allowed = set(RunState.__annotations__)
    extra = set(state) - allowed
    if extra:
        raise StateViolation(f"그래프 상태에 허용되지 않은 키: {sorted(extra)}")
    result: dict[str, Any] = {}
    for key, value in state.items():
        if key == "step":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_STEP:
                raise StateViolation("step은 0 이상의 작은 정수여야 한다")
        elif key == "next_node":
            if value not in NEXT_NODES:
                raise StateViolation("next_node가 허용된 값이 아니다")
        elif key in _UUID_KEYS:
            if not isinstance(value, str) or len(value) > MAX_STATE_STRING:
                raise StateViolation(f"{key}는 짧은 문자열이어야 한다")
            if value != "":
                try:
                    uuid.UUID(value)
                except ValueError:
                    raise StateViolation(f"{key}는 UUID 문자열이어야 한다") from None
        result[key] = value
    return result
