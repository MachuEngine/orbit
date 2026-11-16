"""에피소드 실행 기록 (ADR-0029 §1). 지표를 나중에 더해도 다시 실행하지 않도록 넓게 남긴다.

결과 행의 `trace` 항목을 만든다.
- `turns`: 턴별 사용자 발화, 비서 답 문장, 구조화된 답(제공자 ID 형식 원본).
- `tool_calls`: 모델이 낸 도구 호출마다 이름, 인자, 결과 본문(모델에 보여 준 JSON. 데이터 봉투를
  씌우기
  전), 오류 종류. 결과 본문은 호출당 `MAX_RESULT_CHARS`자까지다(잘리면 `result_truncated`).
- `answer_handles`: 마지막 유효 `submit_answer`가 가리킨 핸들과 그 아티팩트 ID 대응표.
- `final_text`, `structured`: 최종 답 문장과 구조화된 답.

평가 데이터는 합성이고 `runs/`는 git에서 제외한다(ADR-0029 §1). 실사용 DB·그래프 상태
규칙(ADR-0006)과
무관하다. 결함 주입 여부·규칙 이름·호출 지연은 커넥터 호출 기록(`calls[].fault`,
`calls[].latency_ms`)에
있다. 도구 호출 하나가 커넥터 호출 여럿을 만들 수 있어 둘은 순서로만 대응한다.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# 결과 행 스키마 버전. 1은 `trace`가 없던 v0 결과(행에 이 필드가 없다), 2부터 `trace`와 `task_meta`.
RESULT_SCHEMA_VERSION = 2
MAX_RESULT_CHARS = 8_000
SUBMIT_ANSWER = "submit_answer"


@dataclass(frozen=True)
class ToolCallTrace:
    name: str
    arguments: Any
    result: str
    is_error: bool
    turn: int | None = None  # 몇 번째 사용자 발화 처리 중인가(0부터). 한 번에 받는 평가 대상은 None


@dataclass
class EpisodeTrace:
    """평가 대상이 에피소드 중에 채우는 기록 수집기. 러너가 만들어 `Episode.trace`로 준다.

    `runflow_v0`의 도구 호출·턴별 답 문장은 러너가 DB에서 읽으므로 이 수집기에는 구조화된 답만
    담긴다.
    """

    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    handles: dict[str, Any] | None = None  # 작업 집합 `export()`
    turn_structured: dict[int, dict[str, Any] | None] = field(default_factory=dict)
    # 평가 전용 Agent 패턴(ADR-0030)의 LLM 호출: 단계 종류(plan·replan·classify·act)와 계획·분류
    # 문장.
    # 문장은 그래프 상태·DB에 두지 않고 결과 파일 trace에만 남긴다(ADR-0029 §1).
    pattern_steps: list[dict[str, Any]] = field(default_factory=list)

    def record_tool(
        self, name: str, arguments: Any, result: str, *, is_error: bool, turn: int | None = None
    ) -> None:
        self.tool_calls.append(ToolCallTrace(name, arguments, result, is_error, turn))

    def record_pattern_step(self, kind: str, text: str | None) -> None:
        """`act`는 종류만 남긴다(문장은 `run_message`에 있다). 계획·분류는 문장도 남긴다."""
        self.pattern_steps.append({"kind": kind, "text": None if kind == "act" else text})

    def record_handles(self, exported: Mapping[str, Any]) -> None:
        self.handles = dict(exported)

    def record_turn_structured(self, turn: int, structured: Mapping[str, Any] | None) -> None:
        self.turn_structured[turn] = None if structured is None else dict(structured)


@dataclass(frozen=True)
class TranscriptTurn:
    """`runflow_v0`의 run 하나(= 사용자 발화 하나)를 DB에서 읽은 기록."""

    user: str
    assistant: str
    tool_calls: tuple[ToolCallTrace, ...]
    handles: Mapping[str, Any] | None = None


def error_kind(is_error: bool, result: str) -> str | None:
    """도구 결과가 `{"error": 종류}`이면 그 종류. 오류가 아니거나 해석할 수 없으면 None."""
    if not is_error:
        return None
    try:
        data = json.loads(result)
    except ValueError:
        return "unparsable"
    if isinstance(data, Mapping) and isinstance(data.get("error"), str):
        return str(data["error"])
    return "unknown"


def _call_dict(call: ToolCallTrace) -> dict[str, Any]:
    text = call.result
    return {
        "turn": call.turn,
        "name": call.name,
        "arguments": call.arguments,
        "result": text[:MAX_RESULT_CHARS],
        "result_chars": len(text),
        "result_truncated": len(text) > MAX_RESULT_CHARS,
        "is_error": call.is_error,
        "error_kind": error_kind(call.is_error, text),
    }


def _handle_values(arguments: Any) -> list[str]:
    """`submit_answer` 인자에서 핸들 문자열을 모은다."""
    if not isinstance(arguments, Mapping):
        return []
    found: list[str] = []
    for key in ("events", "messages"):
        raw = arguments.get(key)
        if isinstance(raw, list | tuple):
            found += [h for h in raw if isinstance(h, str)]
    for key in ("event", "document"):
        if isinstance(arguments.get(key), str):
            found.append(arguments[key])
    return [h.strip().upper() for h in found]


def answer_handle_table(
    calls: Sequence[ToolCallTrace], handles: Mapping[str, Any] | None
) -> dict[str, Any]:
    """마지막 유효 제출이 가리킨 핸들 -> 아티팩트 ID. 일정은 [calendar_id, event_id],
    메일·문서는 ID."""
    submitted = [c for c in calls if c.name == SUBMIT_ANSWER and not c.is_error]
    if not submitted or not handles:
        return {}
    exported: dict[str, Any] = {}
    for group in ("events", "messages", "documents"):
        part = handles.get(group)
        if isinstance(part, Mapping):
            exported.update(part)
    return {h: exported[h] for h in _handle_values(submitted[-1].arguments) if h in exported}


def build_trace(
    *,
    user_turns: Sequence[str],
    final_text: str | None,
    structured: Mapping[str, Any] | None,
    collector: EpisodeTrace,
    transcript: Sequence[TranscriptTurn] | None = None,
) -> dict[str, Any]:
    """결과 행에 넣을 `trace`. `transcript`가 있으면(`runflow_v0`) 턴·도구 호출은 그것을 쓴다."""
    turns: list[dict[str, Any]]
    calls: list[ToolCallTrace]
    handles: Mapping[str, Any] | None
    if transcript is not None:
        turns = [
            {
                "user": t.user,
                "assistant": t.assistant,
                "structured": collector.turn_structured.get(i),
            }
            for i, t in enumerate(transcript)
        ]
        calls = [
            ToolCallTrace(c.name, c.arguments, c.result, c.is_error, i)
            for i, t in enumerate(transcript)
            for c in t.tool_calls
        ]
        handles = next((t.handles for t in reversed(transcript) if t.handles), None)
    else:
        last = len(user_turns) - 1
        turns = [
            {
                "user": u,
                "assistant": final_text if i == last else None,
                "structured": dict(structured) if i == last and structured is not None else None,
            }
            for i, u in enumerate(user_turns)
        ]
        calls = list(collector.tool_calls)
        handles = collector.handles
    return {
        "turns": turns,
        "tool_calls": [_call_dict(c) for c in calls],
        "final_text": final_text,
        "structured": None if structured is None else dict(structured),
        "answer_handles": answer_handle_table(calls, handles),
        "pattern_steps": list(collector.pattern_steps),
    }
