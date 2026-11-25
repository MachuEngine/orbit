"""내장 워커: API lifespan 안에서 작업을 처리하고 정상 종료한다 (ADR-0022 §2). 데이터는 합성이다."""

import asyncio
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from orbit.api.app import create_app
from orbit.core.config import Settings
from orbit.db.session import AsyncDatabase
from orbit.records.jobs import defer_job
from orbit.runflow.jobs import EXECUTE_RUN

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "sim" / "minimal_world.json"


def _llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ORBIT_OPENAI_API_KEY", "synthetic-not-a-key")
    monkeypatch.setenv("ORBIT_LLM_MODEL_ID", "openai/gpt-6-luna")


def _settings(url: str) -> Settings:
    return Settings(
        database_url=SecretStr(url),
        env="dev",
        embedded_worker=True,
        embedding_provider="fake",
        sim_snapshot_path=str(SNAPSHOT),
    )


async def _status(db: AsyncDatabase, run_id: uuid.UUID) -> str | None:
    async with db.workspace_session(uuid.uuid4()) as s:
        row = (
            await s.execute(
                text("SELECT status::text FROM procrastinate_jobs WHERE args->>'run_id' = :r"),
                {"r": str(run_id)},
            )
        ).first()
    return None if row is None else str(row[0])


async def test_embedded_worker_processes_jobs_and_stops_on_shutdown(
    app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _llm_env(monkeypatch)
    app = create_app(_settings(app_url))
    run_id, ws = uuid.uuid4(), uuid.uuid4()
    async with app.router.lifespan_context(app):
        db: AsyncDatabase = app.state.db
        async with db.workspace_session(ws) as s:
            # 없는 run이라 처리기는 아무것도 하지 않고 정상 종료한다(멱등 진입점)
            await defer_job(
                s,
                app.state.jobs,
                task_name=EXECUTE_RUN,
                args={"run_id": str(run_id), "workspace_id": str(ws)},
            )
        for _ in range(100):
            if await _status(db, run_id) == "succeeded":
                break
            await asyncio.sleep(0.1)
        assert await _status(db, run_id) == "succeeded"  # 같은 프로세스의 워커가 처리했다
    # lifespan 종료 뒤 워커 태스크가 남아 있지 않다
    leftovers = [t for t in asyncio.all_tasks() if t.get_name() == "embedded-worker"]
    assert leftovers == []
    async with AsyncDatabase.from_url(app_url).workspace_session(ws) as s:
        await s.execute(
            text("DELETE FROM procrastinate_jobs WHERE args->>'run_id' = :r"), {"r": str(run_id)}
        )


async def test_embedded_worker_refuses_to_start_without_llm_settings(
    app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ORBIT_LLM_MODEL_ID", "ORBIT_LLM_PROVIDER_ORDER", "ORBIT_OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORBIT_LLM_PROVIDER", "openrouter")
    app = create_app(_settings(app_url))
    with pytest.raises(RuntimeError, match="ORBIT_LLM_MODEL_ID"):
        async with app.router.lifespan_context(app):
            pass
