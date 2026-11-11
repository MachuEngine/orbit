"""미리 정한 응답을 순서대로 돌려주는 LLM. 네트워크 없음, 결정적 (ADR-0009 §1)."""

from collections.abc import Iterable

from orbit.llm.interface import LLMError
from orbit.llm.types import LLMRequest, LLMResponse


class ScriptExhaustedError(LLMError):
    pass


class ScriptedLLM:
    """`responses`의 각 항목은 `LLMResponse`(반환) 또는 `Exception`(발생)이다."""

    def __init__(self, responses: Iterable[LLMResponse | Exception]) -> None:
        self._responses = list(responses)
        self._next = 0
        self.requests: list[LLMRequest] = []

    @property
    def remaining(self) -> int:
        return len(self._responses) - self._next

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._next >= len(self._responses):
            raise ScriptExhaustedError(
                f"스크립트 응답이 소진됐다: {len(self._responses)}개를 모두 썼는데 "
                f"{len(self.requests)}번째 요청이 왔다"
            )
        item = self._responses[self._next]
        self._next += 1
        if isinstance(item, Exception):
            raise item
        return item
