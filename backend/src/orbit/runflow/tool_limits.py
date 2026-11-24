"""평가 전용 도구 제한과 맥락 상한(ADR-0036 §A2).

`RunDeps.tool_limits`가 있을 때만 켜진다. 허용 도구 밖의 호출은 모델에게 보이는 도구 목록에서
빼고, 실행 코드에서도 실행하지 않고 거부로 기록한다. 도구 결과로 받은 본문 글자 수는 한 에피소드
안에서 하나의 상한을 공유하고, 넘으면 잘라 기록한다. 이 객체는 메모리에만 있고, 본문·주소·이름은
담지 않는다(ADR-0006). 메일 ID는 결과 파일용 진단(`diag`)에만 나간다.
"""

import contextlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from orbit.assistant.tools import ToolOutcome
from orbit.assistant.workset import UnknownHandleError, WorkSet
from orbit.connectors.contracts import MailConnector

# (인자, workspace_id, 작업 집합, 메일 커넥터) -> 검색 결과. 평가 전용 검색 실행 대체(ADR-0038)
SearchOverride = Callable[[object, uuid.UUID, WorkSet, MailConnector], Awaitable[ToolOutcome]]

SEARCH_NAME = "search_indexed_mail"
READ_NAME = "read_mail"
_TEXT_FIELD = {SEARCH_NAME: "excerpt", READ_NAME: "body"}
MAX_DENIED_NAME = 64


@dataclass
class ToolLimits:
    allowed: frozenset[str]
    char_cap: int
    used_chars: int = 0
    truncated: bool = False
    search_calls: int = 0
    read_calls: int = 0
    denied: list[str] = field(default_factory=list)
    reached: set[str] = field(default_factory=set)
    index_pending: bool = False
    # 평가 전용(ADR-0038). 기본 None이면 제품 동작과 같다. `search_note`는 도구 설명 끝에 붙는다
    search_override: SearchOverride | None = None
    search_note: str | None = None

    def deny(self, name: str) -> None:
        self.denied.append(name[:MAX_DENIED_NAME])

    def take(self, text: str) -> str:
        """남은 상한만큼만 돌려준다. 잘리면 `truncated`를 켠다."""
        room = max(0, self.char_cap - self.used_chars)
        if len(text) > room:
            self.truncated = True
            text = text[:room]
        self.used_chars += len(text)
        return text

    def filter_prompt(self, system: str, tool_names: list[str]) -> str:
        """시스템 프롬프트에서 허용 밖 도구 이름이 나오는 줄을 뺀다.

        평가 전용이다. 제품 프롬프트는 그대로 둔다.
        """
        denied = [
            re.compile(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])")
            for n in tool_names
            if n not in self.allowed
        ]
        return "\n".join(
            line for line in system.split("\n") if not any(p.search(line) for p in denied)
        )

    def filter_result(self, name: str, content: str, workset: WorkSet) -> str:
        """도구 결과의 본문 필드를 상한에 맞춰 자르고 닿은 메일 ID를 기록한다.

        도달 정의(A0·A2 공통): 본문 또는 excerpt 글자가 1자 이상 모델 입력에 들어간 메일. 원래
        본문이 빈 메일은 머리글이 들어가면 도달로 센다. 상한 때문에 글자가 하나도 못 들어간 메일은
        머리글을 포함해 블록 전체를 결과에서 뺀다(검색은 항목 제거, `read_mail`은 오류 결과).
        """
        if name not in _TEXT_FIELD:
            return content
        if name == SEARCH_NAME:
            self.search_calls += 1
        else:
            self.read_calls += 1
        try:
            data = json.loads(content)
        except ValueError:
            return content
        if not isinstance(data, dict) or "error" in data:
            return content
        if data.get("index_pending"):  # 색인이 덜 준비된 채 답했는지 집계에서 보이게 한다
            self.index_pending = True
        key = _TEXT_FIELD[name]
        entries = data.get("results", []) if name == SEARCH_NAME else [data]
        kept_entries = []
        for entry in entries:
            text = entry.get(key)
            if not isinstance(text, str):
                kept_entries.append(entry)
                continue
            kept = self.take(text)
            if text and not kept:  # 한 글자도 못 들어갔다: 블록 전체를 뺀다
                continue
            if len(kept) < len(text):
                entry[key] = kept
                entry[f"{key}_truncated"] = True
            kept_entries.append(entry)
            with contextlib.suppress(UnknownHandleError):  # 1자 이상 또는 원래 빈 본문
                self.reached.add(workset.resolve_message(entry.get("handle")))
        if name == SEARCH_NAME:
            data["results"] = kept_entries
        elif not kept_entries:
            data = {"error": "context_cap_reached"}
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    def diag(self, arch: str) -> dict[str, Any]:
        """결과 파일에 남기는 진단. 메일 ID·핸들·숫자만 담는다."""
        return {
            "arch": arch,
            "context_message_ids": sorted(self.reached),
            "context_chars": self.used_chars,
            "truncated": self.truncated,
            "char_cap": self.char_cap,
            "search_calls": self.search_calls,
            "read_calls": self.read_calls,
            "denied_tools": list(self.denied),
            "index_pending": self.index_pending,
        }
