"""평가 전용 원인 확인 변형 (ADR-0023 §10). 실사용 경로(워커·API)에서는 쓰지 않는다.

`RunDeps`의 선택 필드로만 켜진다. 끈 상태에서는 도구 목록·시스템 프롬프트·LLM 요청·설정 지문이
변형이 없던 때와 같다.
- `facts-v1`(`RunDeps.facts_extension`): 시스템 프롬프트 끝에 `assistant_v0`의 [사실 확인] 구역과
  상대 날짜·하루 조회 범위 규칙을 한 단락으로 더한다. 줄은 `orbit.assistant.prompt`와 공유한다.
- `reasoning-rt`(`RunDeps.reasoning_cache`): 추론 항목(`provider_items`)을 메모리에만 두고 같은
  run의 다음 step 요청에 되돌린다. 그래프 상태·`run_message`·`tool_call`·작업 인자·로그에는 넣지
  않는다(ADR-0006).
"""

from typing import Any

from orbit.assistant.prompt import DAY_RANGE_RULE, RELATIVE_DATE_LINE, fact_check_lines

FACTS_EXTENSION_VERSION = "facts-v1"
REASONING_ROUNDTRIP_VERSION = "reasoning-rt"

FACTS_PROMPT_PARAGRAPH = (
    "\n".join([*fact_check_lines(), RELATIVE_DATE_LINE, f"- {DAY_RANGE_RULE}"]) + "\n"
)


def with_facts_prompt(system: str) -> str:
    """시스템 프롬프트 끝에 사실 확인 단락을 더한다."""
    return system + "\n" + FACTS_PROMPT_PARAGRAPH


class ReasoningCache:
    """(run_id, step) -> 그 step 응답의 `provider_items`. 메모리에만 두고 에피소드 뒤 버린다."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, int], tuple[dict[str, Any], ...]] = {}

    def put(self, run_id: str, step: int, items: tuple[dict[str, Any], ...]) -> None:
        if items:
            self._items[(run_id, step)] = items

    def for_run(self, run_id: str) -> dict[int, tuple[dict[str, Any], ...]]:
        return {step: items for (rid, step), items in self._items.items() if rid == run_id}
