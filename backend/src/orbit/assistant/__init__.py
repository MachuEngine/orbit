"""읽기 전용 비서 경로 v0 (ADR-0011). 평가 러너에 의존하지 않는다."""

from orbit.assistant.agent import ReadOnlyAssistant
from orbit.assistant.prompt import PROMPT_VERSION, build_system_prompt
from orbit.assistant.tools import ReadOnlyTools, tool_specs
from orbit.assistant.workset import UnknownHandleError, WorkSet

__all__ = [
    "PROMPT_VERSION",
    "ReadOnlyAssistant",
    "ReadOnlyTools",
    "UnknownHandleError",
    "WorkSet",
    "build_system_prompt",
    "tool_specs",
]
