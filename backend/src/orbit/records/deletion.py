"""run 삭제 전파 (ADR-0006 결정 3, ADR-0012 §1).

`run` 행을 지우면 하위 행(run_event, run_message, tool_call, approval, execution)은 복합 FK의
ON DELETE CASCADE로 같이 지워진다. LangGraph 체크포인트는 RLS 밖 라이브러리 테이블이라
같은 트랜잭션 안에서 `thread_id = run_id`로 따로 지운다. 한쪽만 지워지는 상태가 남지 않는다.

체크포인트 테이블 이름은 아래 상수에 둔다. 테이블이 아직 없으면(LangGraph 설정 전)
건너뛴다. 테이블이
있는데 `orbit_app`에 DELETE 권한이 없으면 오류가 나게 둔다(조용히 남기지 않는다). 권한 부여는
체크포인터를 설치하는 쪽(4C)이 한다.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


async def delete_run(session: AsyncSession, *, workspace_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """run과 하위 행, 체크포인트를 지운다. 이 워크스페이스에 run이 있었으면 True."""
    deleted = (
        await session.execute(
            text("DELETE FROM run WHERE id = :id AND workspace_id = :ws RETURNING id"),
            {"id": run_id, "ws": workspace_id},
        )
    ).first()
    if deleted is None:
        # 다른 워크스페이스의 run_id로 체크포인트를 지우지 못하게, run을 지운 경우에만 진행한다.
        return False
    for table in CHECKPOINT_TABLES:
        exists = (
            await session.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}
            )
        ).scalar_one()
        if exists:
            await session.execute(
                text(f"DELETE FROM public.{table} WHERE thread_id = :tid"),  # noqa: S608 - 상수 이름
                {"tid": str(run_id)},
            )
    return True
