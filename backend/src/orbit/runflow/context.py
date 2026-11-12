"""같은 대화의 앞선 run에서 모델 입력 맥락을 만든다 (ADR-0017 §1·§2, ADR-0006).

맥락은 노드에 진입할 때마다 `run_message`(RLS)에서 다시 만든다. 그래프 상태·작업 인자에는 ID만 둔다.
앞선 run의 도구 호출·결과 원문은 모델 입력에 넣지 않고, 도구 결과에서는 핸들과 짧은 설명만 뽑는다.
상수는 `실험 중` 기본값이며(ADR-0017) `run.config_fingerprint`에 포함한다.
"""

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.assistant.tools import wrap_external
from orbit.assistant.workset import WorkSet
from orbit.llm.types import Message
from orbit.records.runs import TERMINAL_RUN_STATUSES, RunMessageRecord

RECENT_TURNS = 6
WORKSET_TOP_K = 20
OLDER_REQUESTS_MAX = 10
OLDER_REQUEST_CHARS = 80
TURN_TEXT_CHARS = 2000
PRIOR_RUNS_MAX = 30  # 노드 진입마다 읽는 앞선 run 수(ADR-0017 §1 읽는 범위)
TARGET_FIELD_CHARS = 80  # [참조 가능한 대상]의 제목·이름 같은 외부 텍스트 상한

CONTEXT_CONFIG: dict[str, int] = {
    "recent_turns": RECENT_TURNS,
    "workset_top_k": WORKSET_TOP_K,
    "older_requests_max": OLDER_REQUESTS_MAX,
    "older_request_chars": OLDER_REQUEST_CHARS,
    "turn_text_chars": TURN_TEXT_CHARS,
    "target_field_chars": TARGET_FIELD_CHARS,
    "prior_runs_max": PRIOR_RUNS_MAX,
}

_HANDLE = re.compile(r"[EM][1-9][0-9]*")


@dataclass(frozen=True)
class Turn:
    user: str
    assistant: str | None


@dataclass(frozen=True)
class ConversationContext:
    """같은 대화의 앞선 종료 run들의 메시지 행. run은 오래된 순이다."""

    runs: tuple[tuple[RunMessageRecord, ...], ...] = ()
    # 읽는 범위와 상관없이 대화의 가장 최근 핸들 기록(작업 집합 매핑). 없으면 None.
    handles: dict[str, Any] | None = None

    @property
    def rows(self) -> tuple[RunMessageRecord, ...]:
        return tuple(row for run in self.runs for row in run)

    def turns(self) -> list[Turn]:
        turns: list[Turn] = []
        for run in self.runs:
            users = [str(r.content.get("text", "")) for r in run if r.role == "user"]
            replies = [
                str(r.content["text"])
                for r in run
                if r.role == "assistant"
                and isinstance(r.content.get("text"), str)
                and r.content["text"].strip()
            ]
            turns.append(
                Turn(
                    user="\n".join(users)[:TURN_TEXT_CHARS],
                    assistant="\n\n".join(replies)[:TURN_TEXT_CHARS] if replies else None,
                )
            )
        return turns

    def latest_workset(self) -> WorkSet | None:
        """대화의 가장 최근 핸들 기록으로 작업 집합을 복원한다. 없으면 None."""
        return None if self.handles is None else WorkSet.restore(self.handles)

    def targets(self) -> list[tuple[str, str]]:
        """(핸들, 설명). 최근에 나온 결과가 먼저이고, 같은 핸들은 가장 최근 등장만 쓴다."""
        found: dict[str, tuple[int, int, str]] = {}  # 핸들 -> (결과 순번, 결과 안 순서, 설명)
        index = 0
        for row in self.rows:
            if row.role != "tool" or row.content.get("is_error"):
                continue
            try:
                payload = json.loads(str(row.content.get("text", "")))
            except ValueError:
                continue
            index += 1
            for position, item in enumerate(_handle_objects(payload)):
                handle = str(item["handle"]).upper()
                found[handle] = (index, position, _describe(handle, item))
        ordered = sorted(found.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
        return [(handle, desc) for handle, (_, _, desc) in ordered[:WORKSET_TOP_K]]

    def messages(self) -> list[Message]:
        """시스템 프롬프트 뒤, 지금 run의 메시지 앞에 들어갈 메시지. 앞선 run이 없으면 비어 있다."""
        turns = self.turns()
        if not turns:
            return []
        older, recent = turns[:-RECENT_TURNS], turns[-RECENT_TURNS:]
        blocks: list[str] = []
        requests = [_one_line(t.user, OLDER_REQUEST_CHARS) for t in reversed(older)]
        requests = [r for r in requests if r][:OLDER_REQUESTS_MAX]
        if requests:
            blocks.append("[이전 대화]\n" + "\n".join(f"- {r}" for r in requests))
        targets = self.targets()
        if targets:
            blocks.append("[참조 가능한 대상]\n" + "\n".join(f"- {h}: {d}" for h, d in targets))
        messages: list[Message] = []
        if blocks:
            messages.append(Message(role="system", content="\n\n".join(blocks)))
        for turn in recent:
            messages.append(Message(role="user", content=turn.user))
            if turn.assistant is not None:
                messages.append(Message(role="assistant", content=turn.assistant))
        return messages


def _one_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _handle_objects(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        handle = node.get("handle")
        if isinstance(handle, str) and _HANDLE.fullmatch(handle.strip().upper()):
            out.append({**node, "handle": handle.strip().upper()})
        for value in node.values():
            if isinstance(value, dict | list):
                out.extend(_handle_objects(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_handle_objects(item))
    return out


def _ext(value: Any) -> str | None:
    if isinstance(value, dict):  # {"name", "email"}
        value = value.get("name") or value.get("email")
    if not isinstance(value, str) or not value.strip():
        return None
    return wrap_external(_one_line(value, TARGET_FIELD_CHARS))[0]


def _describe(handle: str, item: dict[str, Any]) -> str:
    if handle.startswith("E"):
        parts = [
            _ext(item.get("title")),
            _plain(item.get("start")),
            _plain(item.get("end")),
        ]
        start, end = parts[1], parts[2]
        when = " ~ ".join(p for p in (start, end) if p)
        return " ".join(p for p in (parts[0], when) if p) or "(설명 없음)"
    sender = _ext(item.get("from"))
    parts2 = [
        _ext(item.get("subject")),
        f"보낸 사람 {sender}" if sender else None,
        _plain(item.get("date")),
    ]
    return " ".join(p for p in parts2 if p) or "(설명 없음)"


def _plain(value: Any) -> str | None:
    """시각 문자열. 외부 텍스트가 아닌 서버 포맷 값이지만 길이는 제한한다."""
    return value[:40] if isinstance(value, str) and value else None


async def load_context(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> ConversationContext:
    """같은 대화에서 지금 run보다 먼저 만들어진 종료 run을 읽는다.

    메시지는 최근 `PRIOR_RUNS_MAX`개 run만 오래된 순으로 읽는다. 핸들 기록은 범위와 상관없이
    가장 최근 한 행을 따로 읽는다.
    """
    params = {"run": run_id, "ws": workspace_id, "terminal": sorted(TERMINAL_RUN_STATUSES)}
    prior = (
        "FROM run r JOIN run cur ON cur.id = :run AND cur.workspace_id = :ws"
        " WHERE r.workspace_id = :ws AND r.conversation_id = cur.conversation_id"
        " AND r.status = ANY(CAST(:terminal AS text[]))"
        " AND (r.created_at, r.id) < (cur.created_at, cur.id)"
    )
    rows = await session.execute(
        text(
            "SELECT p.id AS run_id, m.seq, m.role, m.content, m.created_at FROM ("  # noqa: S608
            f" SELECT r.id, r.created_at {prior}"
            " ORDER BY r.created_at DESC, r.id DESC LIMIT :limit) p"
            " JOIN run_message m ON m.run_id = p.id AND m.workspace_id = :ws"
            " ORDER BY p.created_at, p.id, m.seq"
        ),
        {**params, "limit": PRIOR_RUNS_MAX},
    )
    grouped: dict[uuid.UUID, list[RunMessageRecord]] = {}
    for r in rows:
        grouped.setdefault(r.run_id, []).append(
            RunMessageRecord(r.seq, r.role, r.content, r.created_at)
        )
    latest = (
        await session.execute(
            text(
                "SELECT m.content->'handles' AS handles FROM ("  # noqa: S608
                f" SELECT r.id, r.created_at {prior}) p"
                " JOIN run_message m ON m.run_id = p.id AND m.workspace_id = :ws"
                " AND m.role = 'tool' AND m.content->'handles' IS NOT NULL"
                " ORDER BY p.created_at DESC, p.id DESC, m.seq DESC LIMIT 1"
            ),
            params,
        )
    ).first()
    return ConversationContext(
        tuple(tuple(v) for v in grouped.values()), None if latest is None else latest.handles
    )
