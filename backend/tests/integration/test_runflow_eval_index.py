"""`runflow_v0 --index on` 통합 테스트 (ADR-0023 §8, ADR-0028). 데이터는 모두 합성 값이다.

실제 PostgreSQL(`orbit_test`)에 평가용 색인을 만들고, `FakeEmbedder` 기반 임베더와 `ScriptedLLM`으로
에피소드를 실행한다. 실제 임베딩·LLM 호출은 없다.
"""

import copy
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from integration.mailindex_support import CountingEmbedder
from integration.support import WorkspaceFactory
from orbit.core.clock import FixedClock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.evaldata.schema import Gold, Task, Turn
from orbit.evalrunner.llm_script import ScriptedLLMFactory
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.embed_cache import CachingEmbedder
from orbit.evalrunner.runflow_env import RunflowEnvironment
from orbit.evalrunner.runflow_index import (
    EvalIndexProvider,
    IndexNotBuilt,
    PinnedWorkspaceDatabase,
    UnsupportedSessionUse,
    WrongWorkspace,
    build_eval_indexes,
)
from orbit.evalrunner.runner import ExperimentSummary, run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import Dataset, load_dataset
from orbit.llm.scripted import ScriptedLLM

BASE = Path(__file__).resolve().parents[1] / "unit" / "evalrunner" / "fixtures" / "dataset"
KST = timezone(timedelta(hours=9))
AS_OF = datetime(2026, 11, 11, 9, 0, tzinfo=KST)
USER_PREFIX = "eval-retrieval-"

ALICE_MAIL = {
    "subject": "SYNTH-ALICE 견적서 납기 협의",
    "body": "SYNTH-ALICE-BODY 요청하신 견적서의 납기는 다음 주 금요일입니다.",
}
BOB_MAIL = {
    "subject": "SYNTH-BOB 점심 메뉴 안내",
    "body": "SYNTH-BOB-BODY 이번 주 점심은 김밥과 우동입니다.",
}
OTHER_FP_MAIL = {
    "subject": "SYNTH-OTHERFP 다른 지문 전용 메일",
    "body": "SYNTH-OTHERFP-BODY 이 메일은 다른 지문의 색인에만 있다.",
}


def add_mail(world: dict[str, Any], account: str, mail_id: str, spec: Mapping[str, str]) -> None:
    state = world["accounts"][account]
    state["threads"].append({"thread_id": f"thr-{mail_id}", "message_ids": [mail_id]})
    state["messages"].append(
        {
            "id": mail_id,
            "thread_id": f"thr-{mail_id}",
            "from": {"email": "sender@example.org", "name": "합성 발신자"},
            "to": [{"email": world["accounts"][account]["email"], "name": "수신자"}],
            "cc": [],
            "subject": spec["subject"],
            "body_text": spec["body"],
            "snippet": spec["body"][:40],
            "sent_at": "2026-11-05T09:00:00+00:00",
            "labels": ["INBOX"],
            "attachments": [],
            "headers": {},
        }
    )


def make_world() -> dict[str, Any]:
    world = copy.deepcopy(dict(load_dataset(BASE).world_snapshot))
    add_mail(world, "acct-alice", "msg-synth-alice", ALICE_MAIL)
    add_mail(world, "acct-bob", "msg-synth-bob", BOB_MAIL)
    return world


def task(task_id: str, account: str = "acct-alice") -> Task:
    return Task.model_validate(
        {
            "task_id": task_id,
            "scenario_id": f"s-{task_id}",
            "cluster_id": f"c-{task_id}",
            "split": "dev",
            "as_of_time": AS_OF,
            "account_id": account,
            "turns": [Turn(role="user", content="견적서 납기를 찾아 주세요")],
            "gold": Gold(),
        }
    )


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def search_script(query: str, *, read: bool = False) -> list[dict[str, Any]]:
    script: list[dict[str, Any]] = [{"tool_calls": [call("search_indexed_mail", query=query)]}]
    if read:
        script.append({"tool_calls": [call("read_mail", handle="M1")]})
    script.append({"text": "확인했습니다."})
    return script


class Factory(ScriptedLLMFactory):
    def __init__(self, script: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        super().__init__(script)
        self.made: dict[str, ScriptedLLM] = {}

    def __call__(self, task: Any, repeat: int) -> ScriptedLLM:
        llm = super().__call__(task, repeat)
        assert isinstance(llm, ScriptedLLM)
        self.made[task.task_id] = llm
        return llm

    def tool_results(self, task_id: str) -> list[str]:
        """모델이 받은 도구 결과 본문(요청 순서)."""
        last = self.made[task_id].requests[-1]
        return [str(m.content) for m in last.messages if m.role == "tool"]


@dataclass
class Env:
    app_url: str
    owner_url: str
    db: AsyncDatabase
    workspaces: WorkspaceFactory
    tmp: Path
    embedder: CountingEmbedder
    cache: CachingEmbedder
    world_sha: str

    async def build(
        self, world: dict[str, Any], accounts: Sequence[str], sha: str | None = None
    ) -> EvalIndexProvider:
        provider, _ = await build_eval_indexes(
            world_snapshot=world,
            world_sha256=sha or self.world_sha,
            accounts=accounts,
            app_url=self.app_url,
            owner_url=self.owner_url,
            embedder=self.cache,
        )
        for handle in provider.handles.values():
            self.workspaces.created.append(handle.workspace_id)  # 테스트가 끝나면 지운다
        return provider

    async def run(
        self,
        tasks: Sequence[Task],
        script: Mapping[str, Sequence[Mapping[str, Any]]],
        provider: EvalIndexProvider | None,
        world: dict[str, Any],
    ) -> tuple[ExperimentSummary, Factory]:
        base = load_dataset(BASE)
        data = Dataset(base.name, base.version, base.manifest, world, tuple(tasks))
        factory = Factory(script)
        mode = "off" if provider is None else "on"
        summary = await run_experiment(
            data,
            split="dev",
            sut_factory=lambda: create_sut("runflow_v0", index_mode=mode, index=provider),
            llm_factory=factory,
            repeats=1,
            git_sha="test-sha",
            database_env=RunflowEnvironment(self.app_url),
        )
        return summary, factory


@pytest.fixture
async def ix(
    app_url: str,
    owner_url: str,
    async_db: AsyncDatabase,
    workspaces: WorkspaceFactory,
    owner_engine: Engine,
    tmp_path: Path,
) -> AsyncIterator[Env]:
    embedder = CountingEmbedder()
    env = Env(
        app_url=app_url,
        owner_url=owner_url,
        db=async_db,
        workspaces=workspaces,
        tmp=tmp_path,
        embedder=embedder,
        cache=CachingEmbedder(embedder, tmp_path),
        world_sha=f"test-world-{uuid.uuid4()}",  # 테스트마다 다른 지문 -> 다른 색인
    )
    yield env
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM app_user WHERE google_sub LIKE :p"), {"p": USER_PREFIX + "%"}
        )


# ---------------------------------------------------------------- 검색과 읽기


async def test_on_search_returns_results_and_handle_reads_the_mail(ix: Env) -> None:
    world = make_world()
    provider = await ix.build(world, ["acct-alice"])
    summary, factory = await ix.run(
        [task("t-1")], {"t-1": search_script("견적서 납기", read=True)}, provider, world
    )
    assert summary.results[0].failure_type is None
    search_result, read_result = factory.tool_results("t-1")
    found = json.loads(_payload(search_result))
    subjects = [r["subject"] for r in found["results"]]
    assert ALICE_MAIL["subject"] in subjects
    assert all(r["handle"].startswith("M") for r in found["results"])
    assert "index_empty" not in search_result and "index_unavailable" not in search_result
    assert found["indexed_as_of"].startswith("2026-11-16")  # 고정 색인 시각(KST 표기)
    assert "index_pending" not in found  # 색인이 완성된 상태
    assert ALICE_MAIL["body"] in read_result  # 핸들로 read_mail이 이어진다
    row = summary.results[0].to_dict()
    assert row["config"]["sut"]["version"].endswith("+index-on")
    info = row["config"]["sut_config"]
    assert info["index_mode"] == "on"
    assert info["index"]["fingerprints"] == {
        "acct-alice": provider.handles["acct-alice"].fingerprint
    }
    assert info["index"]["embedding_model_id"] == ix.embedder.model_id
    assert info["index"]["sync_before_query"] is False


async def test_off_is_unchanged_and_on_is_a_different_experiment(ix: Env) -> None:
    world = make_world()
    off, off_factory = await ix.run([task("t-1")], {"t-1": search_script("견적서")}, None, world)
    provider = await ix.build(world, ["acct-alice"])
    on, _ = await ix.run([task("t-1")], {"t-1": search_script("견적서")}, provider, world)
    assert "index_unavailable" in off_factory.tool_results("t-1")[0]
    off_cfg = off.results[0].to_dict()["config"]
    assert "index" not in off_cfg["sut_config"] and off_cfg["sut_config"]["index_mode"] == "off"
    assert off.results[0].config_fingerprint != on.results[0].config_fingerprint


# ---------------------------------------------------------------- 격리


async def test_each_account_searches_only_its_own_index(ix: Env) -> None:
    world = make_world()
    provider = await ix.build(world, ["acct-alice", "acct-bob"])
    assert provider.handles["acct-alice"].workspace_id != provider.handles["acct-bob"].workspace_id
    script = {
        "a-1": search_script("견적서 납기 점심 김밥"),
        "b-1": search_script("견적서 납기 점심 김밥"),
    }
    _, factory = await ix.run([task("a-1"), task("b-1", "acct-bob")], script, provider, world)
    alice = json.loads(_payload(factory.tool_results("a-1")[0]))["results"]
    bob = json.loads(_payload(factory.tool_results("b-1")[0]))["results"]
    alice_subjects = {r["subject"] for r in alice}
    bob_subjects = {r["subject"] for r in bob}
    assert ALICE_MAIL["subject"] in alice_subjects and BOB_MAIL["subject"] not in alice_subjects
    assert BOB_MAIL["subject"] in bob_subjects and ALICE_MAIL["subject"] not in bob_subjects


async def test_index_with_another_fingerprint_is_not_mixed_in(ix: Env) -> None:
    world = make_world()
    provider = await ix.build(world, ["acct-alice"])
    other_world = copy.deepcopy(world)  # 같은 계정, 다른 지문(세계 해시), 메일 하나가 더 있다
    add_mail(other_world, "acct-alice", "msg-synth-otherfp", OTHER_FP_MAIL)
    other = await ix.build(other_world, ["acct-alice"], sha=ix.world_sha + "-other")
    assert other.handles["acct-alice"].fingerprint != provider.handles["acct-alice"].fingerprint
    query = "다른 지문 전용 메일 견적서 납기"
    _, factory = await ix.run([task("t-1")], {"t-1": search_script(query)}, provider, world)
    subjects = [
        r["subject"] for r in json.loads(_payload(factory.tool_results("t-1")[0]))["results"]
    ]
    assert subjects and OTHER_FP_MAIL["subject"] not in subjects
    _, other_factory = await ix.run([task("t-1")], {"t-1": search_script(query)}, other, world)
    found = json.loads(_payload(other_factory.tool_results("t-1")[0]))["results"]
    assert OTHER_FP_MAIL["subject"] in [r["subject"] for r in found]  # 대조: 그 색인에는 있다


async def test_pinned_database_rejects_foreign_workspace_and_reads_index_rows(ix: Env) -> None:
    world = make_world()
    provider = await ix.build(world, ["acct-alice", "acct-bob"])
    alice = provider.handles["acct-alice"]
    episode_ws = uuid.uuid4()
    pinned = PinnedWorkspaceDatabase(
        ix.db, episode_workspace=episode_ws, index_workspace=alice.workspace_id
    )
    async with pinned.workspace_session(episode_ws) as s:
        count = (await s.execute(text("SELECT count(*) FROM source_item"))).scalar_one()
    assert count == alice.indexed_mails  # RLS가 색인 워크스페이스 행만 보여 준다(bob 행 없음)
    for other in (alice.workspace_id, provider.handles["acct-bob"].workspace_id, uuid.uuid4()):
        with pytest.raises(WrongWorkspace):
            async with pinned.workspace_session(other):
                pass


async def test_unknown_account_has_no_index(ix: Env) -> None:
    provider = await ix.build(make_world(), ["acct-alice"])
    clock = FixedClock(AS_OF)
    with pytest.raises(IndexNotBuilt):
        provider.mail_index_for(
            account_id="acct-bob",
            db=ix.db,
            clock=clock,
            ids=Uuid7Generator(clock),
            episode_workspace=uuid.uuid4(),
        )


# ---------------------------------------------------------------- 재사용·고정 시점


async def test_reuse_embeds_nothing_and_keeps_fingerprint(ix: Env) -> None:
    world = make_world()
    first = await ix.build(world, ["acct-alice", "acct-bob"])
    assert not any(h.reused for h in first.handles.values())
    built = ix.embedder.embedded
    assert built > 0
    again = await ix.build(world, ["acct-alice", "acct-bob"])
    assert all(h.reused for h in again.handles.values())
    assert ix.embedder.embedded == built  # 재사용 시 임베딩 0회
    assert again.config_info() == first.config_info()
    # 새 캐시(파일 비움)에서도 색인이 완성돼 있으면 임베딩하지 않는다
    fresh = CachingEmbedder(ix.embedder, ix.tmp / "empty-cache")
    _, _ = await build_eval_indexes(
        world_snapshot=world,
        world_sha256=ix.world_sha,
        accounts=["acct-alice"],
        app_url=ix.app_url,
        owner_url=ix.owner_url,
        embedder=fresh,
    )
    assert ix.embedder.embedded == built and fresh.misses == 0


async def test_query_does_not_sync_or_change_the_index(ix: Env) -> None:
    world = make_world()
    provider = await ix.build(world, ["acct-alice"])
    handle = provider.handles["acct-alice"]

    async def state() -> tuple[Any, ...]:
        async with ix.db.workspace_session(handle.workspace_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT last_synced_at, history_id FROM sync_state WHERE workspace_id = :ws"
                    ),
                    {"ws": handle.workspace_id},
                )
            ).one()
        return tuple(row)

    before = await state()
    await ix.run([task("t-1")], {"t-1": search_script("견적서 납기")}, provider, world)
    assert await state() == before
    assert before[0] == worldmail.INDEX_NOW


async def test_remapped_session_reads_only_the_pinned_index_rows(ix: Env) -> None:
    provider = await ix.build(make_world(), ["acct-alice", "acct-bob"])
    alice, bob = provider.handles["acct-alice"], provider.handles["acct-bob"]
    episode_ws = uuid.uuid4()
    pinned = PinnedWorkspaceDatabase(
        ix.db, episode_workspace=episode_ws, index_workspace=alice.workspace_id
    )
    count = "SELECT count(*) FROM source_item WHERE workspace_id = :ws"
    async with pinned.workspace_session(episode_ws) as s:
        mine = (await s.execute(text(count), {"ws": episode_ws})).scalar_one()  # 인자가 바뀐다
        theirs = (await s.execute(text(count), {"ws": bob.workspace_id})).scalar_one()
        everything = (await s.execute(text("SELECT count(*) FROM source_item"))).scalar_one()
        with pytest.raises(UnsupportedSessionUse):
            await s.commit()  # execute 밖의 기능은 크게 실패한다
        with pytest.raises(TypeError):
            await s.execute(text("SELECT 1"), 5)  # type: ignore[call-overload]
    assert mine == alice.indexed_mails
    assert theirs == 0  # 다른 계정 워크스페이스 행은 RLS로 읽히지 않는다
    assert everything == alice.indexed_mails


async def test_query_embedding_budget_stops_the_experiment(ix: Env) -> None:
    world = make_world()
    provider = await ix.build(world, ["acct-alice"])
    capped = CachingEmbedder(
        CountingEmbedder(), ix.tmp / "capped", max_cost_usd=1e-9, price_per_mtok=1000.0
    )
    limited = EvalIndexProvider(provider.handles, capped)
    script = {"t-1": search_script("새 질의문 견적"), "t-2": search_script("새 질의문 점심")}
    summary, _ = await ix.run([task("t-1"), task("t-2")], script, limited, world)
    assert summary.stopped_reason == "embedding_cost"
    assert summary.results == []  # 에피소드 실패(sut_error)로 섞이지 않는다
    assert limited.budget_error is not None


def _payload(tool_text: str) -> str:
    """도구 결과 메시지에서 JSON 부분만 꺼낸다(데이터 봉투 바깥 문구는 버린다)."""
    start, end = tool_text.index("{"), tool_text.rindex("}")
    return tool_text[start : end + 1]
