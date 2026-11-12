"""대화 리마인더 도구 `create_reminder` (ADR-0021 §5). 실사용 경로(`runflow`)에서만 쓴다.

- Orbit 화면 안 알림만 만들고 외부에는 아무것도 보내지 않는다. 그래서 승인 흐름(ADR-0012)을 거치지
  않는다. 사용자는 알림 목록에서 예정된 리마인더를 보고 취소할 수 있다.
- 인자 검증: `text`는 공백을 뺀 1~200자, `due_at`은 시간대가 있는 ISO 8601이며 지금 이후 1년 이내.
  실패는 `{"error": 종류}`만 돌려준다(입력 원문을 넣지 않는다).
- 알림 ID는 (run, step, 호출 ID)에서 결정적으로 만들고 `ON CONFLICT DO NOTHING`으로 저장한다. 노드를
  재실행해도 리마인더가 두 번 생기지 않는다. 만든 `run_id`를 기록한다.
- 결과는 다른 도구 결과와 같이 LLM 메시지를 만들 때 데이터 봉투로 감싼다(ADR-0018).
- 예정된 대화 리마인더가 `MAX_PENDING_REMINDERS`개면 `limit_reached`다(데이터 속 지시로 인한
  남발 방지).
"""

import json
import uuid
from datetime import datetime, tzinfo
from typing import Any

from orbit.assistant.tools import ToolOutcome
from orbit.core.clock import Clock
from orbit.db.session import AsyncDatabase
from orbit.llm.types import ToolSpec
from orbit.notify import store
from orbit.notify.limits import MAX_PENDING_REMINDERS, MAX_TITLE_CHARS, REMINDER_MAX_AHEAD

CREATE_REMINDER = "create_reminder"
NOTIFICATION_NAMESPACE = uuid.UUID("b3f1c9a2-5d0e-4c71-9a6b-2e8d4f7a1c35")


def reminder_tool_spec() -> ToolSpec:
    return ToolSpec(
        CREATE_REMINDER,
        "사용자가 직접 요청했을 때만 Orbit 알림 목록에 리마인더를 만든다. 외부로 아무것도 "
        "보내지 않으므로 승인이 필요 없다. 사용자는 알림 목록에서 취소할 수 있다. due_at은 "
        "지금보다 뒤이고 1년 이내여야 한다. 실패하면 error 종류를 돌려준다.",
        {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": f"알릴 문구(1~{MAX_TITLE_CHARS}자)",
                },
                "due_at": {
                    "type": "string",
                    "description": "알릴 시각. 오프셋이 있는 ISO 8601(예: 2026-11-13T15:00+09:00)",
                },
            },
            "required": ["text", "due_at"],
            "additionalProperties": False,
        },
    )


def reminder_id(run_id: uuid.UUID, step: int, llm_call_id: str) -> uuid.UUID:
    """(run, step, 호출 ID)에서 결정적으로 만든 알림 ID. 노드를 재실행해도 같은 값이다."""
    return uuid.uuid5(NOTIFICATION_NAMESPACE, f"{run_id}:{step}:{llm_call_id}")


def _error(kind: str) -> ToolOutcome:
    return ToolOutcome(json.dumps({"error": kind}), is_error=True)


class ReminderTool:
    def __init__(self, db: AsyncDatabase, clock: Clock, tz: tzinfo) -> None:
        self._db = db
        self._clock = clock
        self._tz = tz

    @staticmethod
    def _parse(arguments: object) -> tuple[str, datetime] | str:
        """(문구, 시각) 또는 오류 종류."""
        if not isinstance(arguments, dict) or set(arguments) != {"text", "due_at"}:
            return "invalid_arguments"
        text, raw = arguments["text"], arguments["due_at"]
        if not isinstance(text, str) or not isinstance(raw, str):
            return "invalid_arguments"
        text = text.strip()
        if not 1 <= len(text) <= MAX_TITLE_CHARS:
            return "invalid_arguments"
        try:
            due = datetime.fromisoformat(raw.strip())
        except ValueError:
            return "invalid_time"
        if due.tzinfo is None or due.utcoffset() is None:
            return "invalid_time"
        return text, due

    async def execute(
        self,
        arguments: object,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        step: int,
        llm_call_id: str,
    ) -> ToolOutcome:
        parsed = self._parse(arguments)
        if isinstance(parsed, str):
            return _error(parsed)
        text, due = parsed
        now = self._clock.now()
        if due <= now:
            return _error("time_in_past")
        if due > now + REMINDER_MAX_AHEAD:
            return _error("too_far")
        nid = reminder_id(run_id, step, llm_call_id)
        async with self._db.workspace_session(workspace_id) as s:
            # 같은 호출을 재실행한 경우는 이미 저장돼 있다. 상한과 무관하게 성공으로 본다.
            if not await store.reminder_exists(s, workspace_id, nid):
                if (
                    await store.count_pending_reminders(s, workspace_id, now)
                    >= MAX_PENDING_REMINDERS
                ):
                    return _error("limit_reached")
                await store.insert_reminder(
                    s,
                    ws=workspace_id,
                    notification_id=nid,
                    title=text,
                    due_at=due,
                    run_id=run_id,
                    now=now,
                )
        content: dict[str, Any] = {"created": True, "due_at": due.astimezone(self._tz).isoformat()}
        return ToolOutcome(json.dumps(content, ensure_ascii=False, sort_keys=True))
