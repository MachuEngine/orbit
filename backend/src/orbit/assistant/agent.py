"""읽기 전용 비서의 Tool Calling 루프 (ADR-0011).

LangGraph를 쓰지 않는다(읽기 전용 단일 루프에는 체크포인트가 필요 없다, ADR-0011).
LLM 호출과 도구 호출마다 `on_step`을 불러 호출자의 단계 예산과 연결한다.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import tzinfo
from typing import Any

from orbit.assistant.envelope import boundary_for, safe_source, wrap_data
from orbit.assistant.prompt import build_system_prompt
from orbit.assistant.tools import SUBMIT_ANSWER, ReadOnlyTools, tool_specs
from orbit.connectors.contracts import CalendarConnector, MailConnector
from orbit.core.clock import Clock
from orbit.llm.interface import LLM
from orbit.llm.types import LLMRequest, Message

DEFAULT_MAX_TURNS = 20
TEMPERATURE = 0.0  # 재현성을 위해 명시한다(설정 지문에 반영)


class ReadOnlyAssistant:
    """대화 하나를 처리한다. `run`을 부를 때마다 작업 집합(핸들)을 새로 시작한다."""

    def __init__(
        self,
        *,
        llm: LLM,
        calendar: CalendarConnector,
        mail: MailConnector,
        clock: Clock,
        tz: tzinfo,
        on_step: Callable[[], None] | None = None,
        has_step_room: Callable[[], bool] | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        metadata: Mapping[str, str] | None = None,
        boundary_seed: str | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns는 1 이상이어야 한다")
        self._llm = llm
        self._calendar = calendar
        self._mail = mail
        self._clock = clock
        self._tz = tz
        self._on_step = on_step
        self._has_step_room = has_step_room
        self._max_turns = max_turns
        self.text_skipped = False  # 마지막 `run`에서 단계 여유가 없어 보충 호출을 건너뛰었다
        self._metadata = dict(metadata or {})  # 원문 금지. 과제 ID 같은 식별자만.
        # 데이터 봉투 경계값의 씨앗(ADR-0018). 평가는 에피소드 ID라서 같은 에피소드면 같은 값이다.
        self._boundary = boundary_for(
            boundary_seed or f"episode:{self._metadata.get('task_id', '')}"
        )

    def _step(self) -> None:
        if self._on_step is not None:
            self._on_step()

    async def _final_text(self, messages: list[Message]) -> str:
        self._step()
        response = await self._llm.complete(
            LLMRequest(
                messages=tuple(messages),
                purpose="system_under_test",
                tools=(),
                tool_choice="none",
                temperature=TEMPERATURE,
                metadata=dict(self._metadata),
            )
        )
        return response.text or ""

    async def run(self, utterances: Sequence[str]) -> tuple[str, dict[str, Any] | None]:
        """`(final_text, structured)`를 돌려준다.

        유효한 `submit_answer`가 나오면 그 응답을 처리하고 끝낸다. 도구 호출이 없는 응답이 오면
        그 텍스트가 최종 답이다(`structured`는 None). `max_turns`를 넘기면 빈 텍스트로 끝낸다.
        """
        self.text_skipped = False
        tools = ReadOnlyTools(calendar=self._calendar, mail=self._mail, tz=self._tz)
        specs = tool_specs()
        messages: list[Message] = [
            Message(
                role="system",
                content=build_system_prompt(self._clock.now(), self._tz, self._boundary),
            ),
            *(Message(role="user", content=u) for u in utterances),
        ]
        for _ in range(self._max_turns):
            self._step()
            response = await self._llm.complete(
                LLMRequest(
                    messages=tuple(messages),
                    purpose="system_under_test",
                    tools=specs,
                    tool_choice="auto",
                    temperature=TEMPERATURE,
                    metadata=dict(self._metadata),
                )
            )
            if not response.tool_calls:
                return response.text or "", None
            messages.append(
                Message(
                    role="assistant",
                    content=response.text,
                    tool_calls=response.tool_calls,
                    provider_items=response.provider_items,
                )
            )
            structured: dict[str, Any] | None = None
            for call in response.tool_calls:
                if call.name != SUBMIT_ANSWER:  # 제출은 LLM·커넥터 호출이 아니라 단계가 아니다
                    self._step()
                outcome = await tools.execute(call.name, dict(call.arguments))
                if call.name == SUBMIT_ANSWER and outcome.structured is not None:
                    structured = outcome.structured  # 같은 응답의 마지막 유효 제출이 남는다
                messages.append(
                    Message(
                        role="tool",
                        content=wrap_data(
                            outcome.content, source=safe_source(call.name), boundary=self._boundary
                        ),
                        tool_call_id=call.id,
                    )
                )
            if structured is not None:
                text = (response.text or "").strip()
                # 제출 응답에 문장이 없으면 도구 없이 한 번 더 받는다. 단계 여유가 없으면
                # 보충을 건너뛰고 제출한 답을 빈 텍스트로 돌려준다(답을 버리지 않는다).
                if not text:
                    if self._has_step_room is None or self._has_step_room():
                        text = await self._final_text(messages)
                    else:
                        self.text_skipped = True
                return text, structured
        return "", None
