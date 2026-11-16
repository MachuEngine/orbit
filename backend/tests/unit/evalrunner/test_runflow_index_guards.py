"""`--index on`의 기준 시각 검사와 0건 검색 경고 (ADR-0023 §8). 데이터는 합성 값이다."""

import uuid
from datetime import datetime, timedelta

import pytest

from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.schema import Task
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.indexer import IndexHandle
from orbit.evalrunner.runflow_index import AsOfBeforeIndexedMail, EvalIndexProvider, check_as_of
from orbit.evalrunner.runflow_sut import _IndexWatchLLM
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage, Message
from orbit.mailindex.embedding import FakeEmbedder

from .conftest import DATASET_DIR

ACCOUNT = "acct-alice"


def _task(task_id: str, as_of: datetime) -> Task:
    return Task.model_validate(
        {
            "task_id": task_id,
            "scenario_id": f"s-{task_id}",
            "cluster_id": f"c-{task_id}",
            "split": "dev",
            "as_of_time": as_of,
            "account_id": ACCOUNT,
            "turns": [{"role": "user", "content": "안녕"}],
            "gold": {},
        }
    )


def test_as_of_must_not_precede_the_newest_indexed_mail() -> None:
    world = SimWorld.load(DATASET_DIR / "world.json", FixedClock(worldmail.INDEX_NOW))
    newest = max(m.sent_at for m in worldmail.mailbox_messages(world, ACCOUNT))
    check_as_of(world, [_task("ok", newest), _task("later", newest + timedelta(days=1))])
    with pytest.raises(AsOfBeforeIndexedMail, match="1개") as info:
        check_as_of(world, [_task("ok", newest), _task("early", newest - timedelta(seconds=1))])
    assert "early" in str(info.value) and "'ok'" not in str(info.value)


class _Inner:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="x", tool_calls=(), finish_reason="stop", usage=LLMUsage(), cost_usd=None,
            latency_ms=0, model_id="m", provider=None,
        )  # fmt: skip


def _handle(mails: int) -> IndexHandle:
    return IndexHandle(
        account_id=ACCOUNT, fingerprint="f", workspace_id=uuid.uuid4(),
        connection_id=uuid.uuid4(), reused=True, expected_mails=mails, indexed_mails=mails,
        chunks=mails, build_seconds=0.0, embedded_chunks=0, first_sync_max=10,
    )  # fmt: skip


def _tool(call_id: str, payload: str, source: str = "search_indexed_mail") -> Message:
    body = f'<orbit_data source="{source}" boundary="b">\n{payload}\n</orbit_data boundary="b">'
    return Message(role="tool", content=body, tool_call_id=call_id)


async def test_zero_hit_searches_are_counted_once_per_call() -> None:
    provider = EvalIndexProvider({ACCOUNT: _handle(3)}, FakeEmbedder())
    llm = _IndexWatchLLM(_Inner(), provider, ACCOUNT)
    empty, hit = '{"results": [], "indexed_as_of": "x"}', '{"results": [{"handle": "M1"}]}'
    messages = (
        _tool("c1", empty),
        _tool("c2", hit),
        _tool("c3", '{"index_empty": true, "results": []}'),  # 색인 범위 없음은 세지 않는다
        _tool("c4", empty, source="search_mail"),  # 다른 도구
    )
    request = LLMRequest(messages=messages, purpose="system_under_test")
    await llm.complete(request)
    await llm.complete(request)  # 같은 호출 ID는 다시 세지 않는다
    assert provider.zero_hit_searches == 1


async def test_zero_hit_is_not_counted_when_the_index_has_no_mail() -> None:
    provider = EvalIndexProvider({ACCOUNT: _handle(0)}, FakeEmbedder())
    llm = _IndexWatchLLM(_Inner(), provider, ACCOUNT)
    request = LLMRequest(messages=(_tool("c1", '{"results": []}'),), purpose="system_under_test")
    await llm.complete(request)
    assert provider.zero_hit_searches == 0
