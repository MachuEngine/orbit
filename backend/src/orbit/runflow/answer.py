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

from orbit.assistant.answer_guide import answer_guide_lines
from orbit.assistant.tools import SUBMIT_ANSWER, tool_specs
from orbit.llm.types import ToolSpec

ANSWER_EXTENSION_VERSION = "eval-answer-v2"

# 구조화된 답(제공자 ID 기반 ADR-0010 형식)을 받는 쪽. 호출은 동기이고 메모리에만 담는다.
AnswerSink = Callable[[dict[str, Any]], None]

# 안내 본문은 assistant 평가 경로와 같은 함수 출력이다(바이트 단위로 같다).
ANSWER_PROMPT_PARAGRAPH = "[답 제출]\n" + "\n".join(answer_guide_lines()) + "\n"


def answer_tool_spec() -> ToolSpec:
    """평가 경로와 같은 `submit_answer` 정의."""
    return next(spec for spec in tool_specs() if spec.name == SUBMIT_ANSWER)


def with_answer_prompt(system: str) -> str:
    """시스템 프롬프트 끝에 답 제출 안내 한 단락을 더한다."""
    return system + "\n" + ANSWER_PROMPT_PARAGRAPH
