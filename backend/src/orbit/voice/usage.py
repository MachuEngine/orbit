"""음성 사용량 기록과 상한 (ADR-0025 §3, 마이그레이션 0011).

- 행에는 워크스페이스·시각·초·바이트만 있고 오디오·전사 문장은 없다.
- 상한 확인과 기록은 한 트랜잭션에서 한다. 같은 워크스페이스의 동시 요청은 advisory 잠금으로
  직렬화해 상한을 넘겨 통과하지 못하게 한다.
- 기록은 제공자 호출 전에 한다. 호출이 실패해도 행을 남긴다(실패한 시도로 비용·남용 통제를 우회하지
  못하게 한다).
- 정리: 새 기록을 넣을 때 같은 워크스페이스의 `USAGE_RETENTION`보다 오래된 행을 지운다. 사용이
  멈춘 워크스페이스의 행은 남지만 최대 2일 치 이하로 작다(전역 정리 작업은 두지 않는다).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import text

from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.voice.limits import DAY_WINDOW, HOUR_WINDOW, USAGE_RETENTION

Verdict = Literal["ok", "hourly_limit", "daily_limit"]


@dataclass(frozen=True)
class Reservation:
    verdict: Verdict
    usage_id: uuid.UUID | None  # verdict가 ok일 때만 있다


async def reserve(
    db: AsyncDatabase,
    clock: Clock,
    ids: Uuid7Generator,
    ws: uuid.UUID,
    *,
    seconds: float,
    size_bytes: int,
    max_per_hour: int,
    max_seconds_per_day: float,
) -> Reservation:
    now: datetime = clock.now()
    usage_id = ids.next()
    async with db.workspace_session(ws) as s:
        await s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"voice_usage:{ws}"},
        )
        await s.execute(
            text("DELETE FROM voice_usage WHERE workspace_id = :ws AND at < :before"),
            {"ws": ws, "before": now - USAGE_RETENTION},
        )
        hourly = (
            await s.execute(
                text("SELECT count(*) FROM voice_usage WHERE workspace_id = :ws AND at > :since"),
                {"ws": ws, "since": now - HOUR_WINDOW},
            )
        ).scalar_one()
        if int(hourly) >= max_per_hour:
            return Reservation("hourly_limit", None)
        used = (
            await s.execute(
                text(
                    "SELECT COALESCE(sum(seconds), 0) FROM voice_usage"
                    " WHERE workspace_id = :ws AND at > :since"
                ),
                {"ws": ws, "since": now - DAY_WINDOW},
            )
        ).scalar_one()
        if float(used) + seconds > max_seconds_per_day:
            return Reservation("daily_limit", None)
        await s.execute(
            text(
                "INSERT INTO voice_usage (id, workspace_id, at, seconds, bytes)"
                " VALUES (:id, :ws, :now, :seconds, :bytes)"
            ),
            {"id": usage_id, "ws": ws, "now": now, "seconds": seconds, "bytes": size_bytes},
        )
    return Reservation("ok", usage_id)


async def correct(db: AsyncDatabase, ws: uuid.UUID, usage_id: uuid.UUID, *, seconds: float) -> None:
    """제공자가 알려 준 실제 길이로 기록된 초를 바꾼다(같은 워크스페이스 세션 안에서)."""
    async with db.workspace_session(ws) as s:
        await s.execute(
            text("UPDATE voice_usage SET seconds = :seconds WHERE id = :id AND workspace_id = :ws"),
            {"seconds": max(seconds, 0.0), "id": usage_id, "ws": ws},
        )
