"""기준 평가 대상(SUT)과 이름 레지스트리. 러너·테스트 검증용이며 제품 기능이 아니다."""

import json
from collections.abc import Callable

from orbit.connectors.contracts import ConnectorError
from orbit.evalrunner.episode import Episode, EpisodeOutput, SystemUnderTest
from orbit.evalrunner.tools import ToolDispatcher, serialize_result, tool_specs
from orbit.llm.types import LLMRequest, Message


class NoopSUT:
    """아무 도구도 부르지 않고 고정 문장을 돌려준다. 실패 경로 확인용."""

    name = "noop"
    version = "0"

    def __init__(self, text: str = "모르겠습니다.") -> None:
        self._text = text

    async def run(self, episode: Episode) -> EpisodeOutput:
        return EpisodeOutput(final_text=self._text)


class ScriptedToolSUT:
    """LLM이 돌려준 도구 호출을 그대로 실행하고, 도구 호출이 없는 응답의 텍스트를 최종 답으로 쓴다.

    실제 Tool Calling 루프의 골격이다. 도구 오류는 `{"error": "<예외 이름>"}` 메시지로 모델에
    돌려준다(`catch_errors=False`이면 계약 오류를 밖으로 던진다).
    단계는 LLM 호출·도구 호출마다 센다.
    """

    name = "scripted_tool"
    version = "0"

    def __init__(self, *, catch_errors: bool = True, max_turns: int = 20) -> None:
        self._catch_errors = catch_errors
        self._max_turns = max_turns

    async def run(self, episode: Episode) -> EpisodeOutput:
        dispatcher = ToolDispatcher(episode.connectors)
        specs = tool_specs()
        messages: list[Message] = [
            Message(role="user", content=t.content) for t in episode.task.turns
        ]
        for _ in range(self._max_turns):
            episode.steps.consume()
            response = await episode.llm.complete(
                LLMRequest(
                    messages=tuple(messages),
                    purpose="system_under_test",
                    tools=specs,
                    tool_choice="auto",
                    metadata={"task_id": episode.task.task_id},
                )
            )
            if not response.tool_calls:
                return EpisodeOutput(final_text=response.text or "")
            messages.append(
                Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
            )
            for call in response.tool_calls:
                episode.steps.consume()
                try:
                    content = serialize_result(await dispatcher.call(call.name, call.arguments))
                except (ConnectorError, ValueError) as exc:  # ValueError: 입력 위반, 인자 검증 실패
                    if not self._catch_errors and isinstance(exc, ConnectorError):
                        raise
                    content = json.dumps({"error": type(exc).__name__})
                messages.append(Message(role="tool", content=content, tool_call_id=call.id))
        return EpisodeOutput(final_text="")


SutFactory = Callable[[], SystemUnderTest]

_REGISTRY: dict[str, SutFactory] = {"noop": NoopSUT, "scripted_tool": ScriptedToolSUT}


def register_sut(name: str, factory: SutFactory) -> None:
    if name in _REGISTRY:
        raise ValueError(f"이미 등록된 SUT 이름: {name}")
    _REGISTRY[name] = factory


def sut_names() -> list[str]:
    return sorted(_REGISTRY)


class UnknownSutError(ValueError):
    pass


def create_sut(name: str) -> SystemUnderTest:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise UnknownSutError(f"알 수 없는 SUT: {name} (등록: {sut_names()})") from None
    return factory()
