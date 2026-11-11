"""JSON 스크립트에서 `ScriptedLLM` 팩토리를 만든다. 실제 LLM은 부르지 않는다(러너 검증·CLI용).

스크립트 형식: {"<task_id>": [{"text": str|null, "tool_calls": [{"name": str, "arguments": {}}],
"input_tokens": int, "output_tokens": int, "cost_usd": float|null}, ...]}
과제의 스크립트가 없으면 빈 스크립트다(LLM 호출 시 `ScriptExhaustedError`).
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orbit.evalrunner.runner import LLMFactory
from orbit.evalrunner.task_source import TaskView
from orbit.llm.interface import LLM
from orbit.llm.scripted import ScriptedLLM
from orbit.llm.types import LLMResponse, LLMUsage, ToolCall

SCRIPTED_MODEL_ID = "scripted-model"


def build_response(item: Mapping[str, Any]) -> LLMResponse:
    calls = tuple(
        ToolCall(id=f"call-{i}", name=str(c["name"]), arguments=dict(c.get("arguments", {})))
        for i, c in enumerate(item.get("tool_calls", []), start=1)
    )
    cost = item.get("cost_usd")
    return LLMResponse(
        text=item.get("text"),
        tool_calls=calls,
        finish_reason="tool_calls" if calls else "stop",
        usage=LLMUsage(int(item.get("input_tokens", 0)), int(item.get("output_tokens", 0))),
        cost_usd=float(cost) if cost is not None else None,
        latency_ms=0,
        model_id=SCRIPTED_MODEL_ID,
        provider=None,
    )


def scripted_llm_factory(script: Mapping[str, Sequence[Mapping[str, Any]]]) -> LLMFactory:
    def factory(task: TaskView, repeat: int) -> LLM:
        return ScriptedLLM([build_response(i) for i in script.get(task.task_id, [])])

    return factory


def load_script(path: str | Path) -> LLMFactory:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("LLM 스크립트 최상위는 객체여야 한다")
    return scripted_llm_factory(data)
