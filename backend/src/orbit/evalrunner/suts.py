"""기준 평가 대상(SUT)과 이름 레지스트리. 러너·테스트 검증용이며 제품 기능이 아니다."""

import json
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from orbit.assistant.agent import ReadOnlyAssistant
from orbit.assistant.prompt import PROMPT_VERSION
from orbit.connectors.contracts import ConnectorError
from orbit.evalrunner.episode import Episode, EpisodeOutput, SystemUnderTest
from orbit.evalrunner.llm_script import SUBMIT_ANSWER
from orbit.evalrunner.tools import ToolDispatcher, serialize_result, tool_specs
from orbit.llm.types import LLMRequest, Message, ToolSpec


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
        specs = (
            *tool_specs(),
            ToolSpec(SUBMIT_ANSWER, "구조화된 최종 답을 제출한다", {"type": "object"}),
        )
        structured: dict[str, Any] | None = None
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
                return EpisodeOutput(final_text=response.text or "", structured=structured)
            messages.append(
                Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
            )
            submitted = False
            for call in response.tool_calls:
                episode.steps.consume()
                if call.name == SUBMIT_ANSWER:  # 커넥터 호출이 아니다. 마지막 제출이 유효하다
                    structured = dict(call.arguments)
                    submitted = True
                    messages.append(Message(role="tool", content="ok", tool_call_id=call.id))
                    continue
                try:
                    content = serialize_result(await dispatcher.call(call.name, call.arguments))
                except (ConnectorError, ValueError) as exc:  # ValueError: 입력 위반, 인자 검증 실패
                    if not self._catch_errors and isinstance(exc, ConnectorError):
                        raise
                    content = json.dumps({"error": type(exc).__name__})
                messages.append(Message(role="tool", content=content, tool_call_id=call.id))
            if submitted:  # 답 제출이 끝이다
                return EpisodeOutput(final_text=response.text or "", structured=structured)
        return EpisodeOutput(final_text="", structured=structured)


class AssistantV0SUT:
    """제품 경로 `ReadOnlyAssistant`(ADR-0011)를 감싸는 어댑터. 에피소드 입력을 변환할 뿐이다.

    `version`에 프롬프트 버전을 넣어 결과의 설정 지문(`sut.version`)에 남긴다.
    v0는 사용자 시간대를 `Asia/Seoul`로 고정한다. 사용자 발화(`role == "user"`)만 넘긴다.
    """

    name = "assistant_v0"
    version = f"0+{PROMPT_VERSION}"
    TIME_ZONE = "Asia/Seoul"

    def __init__(self, *, max_turns: int = 20) -> None:
        self._max_turns = max_turns

    async def run(self, episode: Episode) -> EpisodeOutput:
        assistant = ReadOnlyAssistant(
            llm=episode.llm,
            calendar=episode.connectors.calendar,
            mail=episode.connectors.mail,
            clock=episode.clock,
            tz=ZoneInfo(self.TIME_ZONE),
            on_step=episode.steps.consume,
            max_turns=self._max_turns,
            metadata={"task_id": episode.task.task_id},
        )
        text, structured = await assistant.run(
            [t.content for t in episode.task.turns if t.role == "user"]
        )
        return EpisodeOutput(final_text=text, structured=structured)


SutFactory = Callable[[], SystemUnderTest]

_REGISTRY: dict[str, SutFactory] = {
    "noop": NoopSUT,
    "scripted_tool": ScriptedToolSUT,
    "assistant_v0": AssistantV0SUT,
}


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
