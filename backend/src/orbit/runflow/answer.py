"""평가 전용 구조화된 답 제출 확장 (ADR-0023 §6). 실사용 경로에서는 쓰지 않는다.

`RunDeps.answer_sink`가 있을 때만 그래프가 이 확장을 켠다. 없으면 도구 목록·시스템 프롬프트·설정
지문이 확장이 없던 때와 같다.
- 도구: 평가 경로(`orbit.assistant.tools`)와 같은 `submit_answer` 정의를 도구 목록 끝에 더한다.
- 프롬프트: 시스템 프롬프트 끝에 답 제출 안내 한 단락을 더한다.
- 제출 결과: 핸들을 제공자 ID로 바꾼 구조화된 답은 `answer_sink`(메모리)로만 넘긴다. 모델에는
  `{"status": "submitted"}`만 돌려주고, 그래프 상태·`run_message`·`tool_call`·작업 인자에는 넣지
  않는다(ADR-0006, D8-1).
"""

from collections.abc import Callable
from typing import Any

from orbit.assistant.tools import SUBMIT_ANSWER, tool_specs
from orbit.llm.types import ToolSpec

ANSWER_EXTENSION_VERSION = "eval-answer-v1"

# 구조화된 답(제공자 ID 기반 ADR-0010 형식)을 받는 쪽. 호출은 동기이고 메모리에만 담는다.
AnswerSink = Callable[[dict[str, Any]], None]

ANSWER_PROMPT_PARAGRAPH = (
    "[답 제출]\n"
    "- 일정이나 메일을 조회해 묻는 질문에 답이 정해지면 submit_answer를 호출해 구조화된 답을 "
    "제출하고, 같은 응답에 한국어로 짧게 답을 적는다. "
    "답 종류(kind)는 event_set(일정 목록을 묻는 질문, events에 해당 일정 핸들을 모두 담고 "
    "없으면 빈 목록), event(특정 일정 하나, event에 핸들), event_time(변경된 일정의 현재 시각, "
    "event와 start), time_slot(빈 시간, start에 가능한 시작 시각)이다. "
    "start는 오프셋이 있는 ISO 8601이다. submit_answer가 오류를 돌려주면 인자를 고쳐 다시 "
    "호출한다. 제출한 뒤에는 도구를 더 부르지 않고 한국어로 짧게 결과를 알린다.\n"
)


def answer_tool_spec() -> ToolSpec:
    """평가 경로와 같은 `submit_answer` 정의."""
    return next(spec for spec in tool_specs() if spec.name == SUBMIT_ANSWER)


def with_answer_prompt(system: str) -> str:
    """시스템 프롬프트 끝에 답 제출 안내 한 단락을 더한다."""
    return system + "\n" + ANSWER_PROMPT_PARAGRAPH
