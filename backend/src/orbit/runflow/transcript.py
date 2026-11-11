"""`run_message`에 저장한 대화(transcript)를 읽는 도구 (ADR-0006, ADR-0012 §1·§4).

대화 원문과 핸들 -> 제공자 ID 매핑은 `run_message`에만 있다. 그래프 노드는 진입할 때마다 이 행들에서
LLM 메시지, 작업 집합(`WorkSet`), 인자 출처 분류 입력을 다시 만든다.

행 내용(`content`) 형식
- user: `{"text"}`
- assistant: `{"step", "text", "tool_calls": [{"id", "name", "arguments"}]}`
- tool: `{"step", "tool_call_id"(LLM이 준 호출 ID), "name", "text"(모델에 준 JSON 문자열),
  "is_error", "handles"(읽기 도구 뒤의 작업 집합 전체 매핑), "orbit_tool_call_id"(쓰기 도구만)}`
LLM이 준 호출 ID는 step마다 겹칠 수 있어 (step, tool_call_id)로 찾는다.
"""

import json
from dataclasses import dataclass
from typing import Any

from orbit.assistant.workset import WorkSet
from orbit.llm.types import Message, ToolCall
from orbit.records.runs import RunMessageRecord

_STRUCTURED_EMAIL_KEYS = frozenset({"from", "to", "cc", "attendees", "organizer"})
_EXTERNAL_TEXT_KEYS = frozenset({"body", "description", "subject", "title", "location", "name"})


@dataclass(frozen=True)
class Transcript:
    rows: tuple[RunMessageRecord, ...]

    def assistant_for_step(self, step: int) -> dict[str, Any] | None:
        for row in self.rows:
            if row.role == "assistant" and row.content.get("step") == step:
                return row.content
        return None

    def tool_results(self, step: int) -> dict[str, dict[str, Any]]:
        """step의 도구 결과 행을 LLM 호출 ID로 찾는다."""
        return {
            str(row.content["tool_call_id"]): row.content
            for row in self.rows
            if row.role == "tool" and row.content.get("step") == step
        }

    def tool_result_for_orbit_call(self, orbit_tool_call_id: str) -> dict[str, Any] | None:
        for row in self.rows:
            if row.role == "tool" and row.content.get("orbit_tool_call_id") == orbit_tool_call_id:
                return row.content
        return None

    def user_utterances(self) -> list[str]:
        return [str(r.content.get("text", "")) for r in self.rows if r.role == "user"]

    def workset(self) -> WorkSet:
        """가장 최근 읽기 도구 결과에 저장된 매핑으로 작업 집합을 복원한다."""
        for row in reversed(self.rows):
            if row.role == "tool" and "handles" in row.content:
                return WorkSet.restore(row.content["handles"])
        return WorkSet()

    def llm_messages(self, system_prompt: str) -> list[Message]:
        messages = [Message(role="system", content=system_prompt)]
        for row in self.rows:
            content = row.content
            if row.role == "user":
                messages.append(Message(role="user", content=str(content.get("text", ""))))
            elif row.role == "assistant":
                calls = tuple(
                    ToolCall(id=str(c["id"]), name=str(c["name"]), arguments=dict(c["arguments"]))
                    for c in content.get("tool_calls", [])
                )
                messages.append(
                    Message(role="assistant", content=content.get("text"), tool_calls=calls)
                )
            elif row.role == "tool":
                messages.append(
                    Message(
                        role="tool",
                        content=str(content.get("text", "")),
                        tool_call_id=str(content["tool_call_id"]),
                    )
                )
        return messages

    def provenance_inputs(self) -> tuple[list[str], list[str]]:
        """(조회 결과의 구조 필드 이메일, 외부 텍스트)를 돌려준다(인자 출처 분류 입력)."""
        emails: list[str] = []
        texts: list[str] = []
        for row in self.rows:
            if row.role != "tool" or row.content.get("is_error"):
                continue
            try:
                payload = json.loads(str(row.content.get("text", "")))
            except ValueError:
                continue
            _collect(payload, emails, texts, in_structured=False)
        return emails, texts


def _collect(node: Any, emails: list[str], texts: list[str], *, in_structured: bool) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "email" and in_structured and isinstance(value, str):
                emails.append(value)
            elif key in _EXTERNAL_TEXT_KEYS and isinstance(value, str):
                texts.append(value)
            else:
                _collect(
                    value,
                    emails,
                    texts,
                    in_structured=in_structured or key in _STRUCTURED_EMAIL_KEYS,
                )
    elif isinstance(node, list):
        for item in node:
            _collect(item, emails, texts, in_structured=in_structured)
