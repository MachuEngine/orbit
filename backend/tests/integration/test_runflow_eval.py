"""`runflow_v0` 평가 대상 통합 테스트 (ADR-0023). 데이터는 모두 합성 값이다.

실제 PostgreSQL(`orbit_test`), 실제 LangGraph 체크포인터를 쓰고 LLM(`ScriptedLLM`)과 커넥터(Sim)만
대체한다. 평가 DB(`orbit_eval`) 대신 같은 스키마의 테스트 DB를 쓴다.
"""

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from integration.runflow_support import DAY, Harness
from orbit.assistant.envelope import boundary_for
from orbit.connectors.contracts import EventDraft
from orbit.evaldata.schema import Gold, Task, Turn
from orbit.evalrunner.episode import Budget, Episode, EpisodeDatabase, EpisodeOutput
from orbit.evalrunner.llm_script import ScriptedLLMFactory
from orbit.evalrunner.runflow_env import (
    DbObservation,
    EpisodeInUse,
    RunflowEnvironment,
    episode_ids,
    lock_key,
)
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.evalrunner.runner import ExperimentSummary, episode_seed, run_experiment
from orbit.evalrunner.stats import failure_distribution
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import Dataset, load_dataset
from orbit.llm.interface import LLM
from orbit.llm.scripted import ScriptedLLM
from orbit.runflow.answer import ANSWER_PROMPT_PARAGRAPH, answer_tool_spec, with_answer_prompt
from orbit.runflow.eval_variants import FACTS_PROMPT_PARAGRAPH, ReasoningCache, with_facts_prompt
from orbit.runflow.fingerprint import config_fingerprint
from orbit.runflow.prompt import PROMPT_VERSION, build_system_prompt
from orbit.runflow.write_tools import run_tool_specs

BASE = Path(__file__).resolve().parents[1] / "unit" / "evalrunner" / "fixtures" / "dataset"
KST = timezone(timedelta(hours=9))
AS_OF = datetime(2026, 11, 11, 9, 0, tzinfo=KST)
READ_ONLY_FORBIDDEN = ["create_event", "update_event", "cancel_event", "create_draft", "send_draft"]
MAIN, TEAM = "cal-alice-main", "cal-alice-team"
EVENT_SET = {
    "kind": "event_set",
    "events": [
        {"calendar_id": MAIN, "event_id": "evalice001"},
        {"calendar_id": MAIN, "event_id": "evalice002"},
    ],
}
CREATE = {
    "title": "합성 점검 회의",
    "start": "2026-11-13T10:00:00+09:00",
    "end": "2026-11-13T11:00:00+09:00",
}
SCHEMA_PROVIDER_IDS = ("evalice001", "evalice002", "evalice003", MAIN, TEAM)


def task(
    task_id: str,
    turns: Sequence[str],
    *,
    gold: Gold | None = None,
    policy: list[str] | None = None,
) -> Task:
    return Task.model_validate(
        {
            "task_id": task_id,
            "scenario_id": f"s-{task_id}",
            "cluster_id": f"c-{task_id}",
            "split": "dev",
            "as_of_time": AS_OF,
            "account_id": "acct-alice",
            "turns": [Turn(role="user", content=t) for t in turns],
            "gold": gold or Gold(),
            "approval_policy": policy,
        }
    )


def dataset(*tasks: Task) -> Dataset:
    base = load_dataset(BASE)
    return Dataset(base.name, base.version, base.manifest, base.world_snapshot, tuple(tasks))


def read_gold(**over: Any) -> Gold:
    fields: dict[str, Any] = {
        "expected_answer": EVENT_SET,
        "expected_changes": [],
        "forbidden_tools": READ_ONLY_FORBIDDEN,
        "required_tool_calls": [{"tool": "list_events", "args_subset": {"calendar_id": MAIN}}],
    }
    fields.update(over)
    return Gold.model_validate(fields)


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def read_script() -> list[dict[str, Any]]:
    """일정을 조회하고 핸들로 구조화된 답을 제출한 뒤 최종 응답을 한다."""
    return [
        {"tool_calls": [call("list_events", **DAY)]},
        {
            "text": "내일은 주간 회의와 점심이 있습니다.",
            "structured": {"kind": "event_set", "events": ["E1", "E2"]},
        },
        {"text": "정리했습니다."},
    ]


def create_script(final: str = "일정을 만들었습니다.") -> list[dict[str, Any]]:
    return [{"tool_calls": [call("create_event", **CREATE)]}, {"text": final}]


class Factory(ScriptedLLMFactory):
    """만든 `ScriptedLLM`을 모아 요청을 확인할 수 있게 한다."""

    def __init__(self, script: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        super().__init__(script)
        self.made: dict[str, ScriptedLLM] = {}

    def __call__(self, task: Any, repeat: int) -> LLM:
        llm = super().__call__(task, repeat)
        assert isinstance(llm, ScriptedLLM)
        self.made[task.task_id] = llm
        return llm


class SpyEnv(RunflowEnvironment):
    """정리 직전에 DB 상태를 점검하고, 정리 뒤 남은 행을 센다. 관찰 직전에 DB를 조작할 수 있다."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.databases: list[EpisodeDatabase] = []
        self.before_cleanup: list[dict[str, Any]] = []
        self.tamper: Callable[[EpisodeDatabase], Awaitable[None]] | None = None

    async def prepare(self, *, as_of: datetime, seed: int) -> EpisodeDatabase:
        database = await super().prepare(as_of=as_of, seed=seed)
        self.databases.append(database)
        return database

    async def observe(self, database: EpisodeDatabase) -> DbObservation:
        if self.tamper is not None:
            await self.tamper(database)
        return await super().observe(database)

    async def cleanup(self, database: EpisodeDatabase) -> None:
        self.before_cleanup.append(await self._inspect(database))
        await super().cleanup(database)

    async def _inspect(self, database: EpisodeDatabase) -> dict[str, Any]:
        ws = database.workspace_id
        async with self.db.workspace_session(ws) as s:
            run_ids = [
                str(r[0])
                for r in await s.execute(
                    text("SELECT id FROM run WHERE workspace_id = :ws"), {"ws": ws}
                )
            ]
            texts = [
                json.dumps(r[0], ensure_ascii=False)
                for r in await s.execute(
                    text("SELECT content FROM run_message WHERE workspace_id = :ws"), {"ws": ws}
                )
            ]
            calls = [
                json.dumps([r[0], r[1]], ensure_ascii=False)
                for r in await s.execute(
                    text("SELECT args, provenance FROM tool_call WHERE workspace_id = :ws"),
                    {"ws": ws},
                )
            ]
            checkpoint_rows = 0
            leaked_in_checkpoints = 0
            readable = 0  # 점검이 내용을 읽을 수 있는지 확인하는 대조군(상태의 run ID)
            for table, column in (
                ("checkpoints", "checkpoint::text::bytea"),
                ("checkpoint_blobs", "blob"),
                ("checkpoint_writes", "blob"),
            ):
                rows = (
                    await s.execute(
                        text(f"SELECT {column} FROM public.{table} WHERE thread_id = ANY(:ids)"),  # noqa: S608
                        {"ids": run_ids},
                    )
                ).all()
                checkpoint_rows += len(rows)
                for (blob,) in rows:
                    raw = bytes(blob) if blob is not None else b""
                    leaked_in_checkpoints += sum(pid.encode() in raw for pid in SCHEMA_PROVIDER_IDS)
                    readable += sum(rid.encode() in raw for rid in run_ids)
        return {
            "run_ids": run_ids,
            "messages": texts,
            "tool_calls": calls,
            "checkpoint_rows": checkpoint_rows,
            "leaked_in_checkpoints": leaked_in_checkpoints,
            "readable_checkpoints": readable,
        }

    async def leftovers(self) -> dict[str, int]:
        """정리 뒤 남은 행. 에피소드마다 워크스페이스와 그 run의 체크포인트를 센다."""
        counts = {
            "workspace": 0,
            "run": 0,
            "checkpoints": 0,
            "checkpoint_blobs": 0,
            "checkpoint_writes": 0,
        }
        for database, before in zip(self.databases, self.before_cleanup, strict=True):
            ws = database.workspace_id
            async with self.db.workspace_session(ws) as s:
                counts["workspace"] += int(
                    (
                        await s.execute(
                            text("SELECT count(*) FROM workspace WHERE id = :ws"), {"ws": ws}
                        )
                    ).scalar_one()
                )
                counts["run"] += int(
                    (
                        await s.execute(
                            text("SELECT count(*) FROM run WHERE workspace_id = :ws"), {"ws": ws}
                        )
                    ).scalar_one()
                )
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    query = f"SELECT count(*) FROM public.{table} WHERE thread_id = ANY(:ids)"  # noqa: S608
                    counts[table] += int(
                        (await s.execute(text(query), {"ids": before["run_ids"]})).scalar_one()
                    )
        return counts


async def run(
    app_url: str,
    tasks: Sequence[Task],
    script: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    sut: Callable[[], Any] | None = None,
    env: SpyEnv | None = None,
    **kwargs: Any,
) -> tuple[ExperimentSummary, SpyEnv, Factory]:
    env = env or SpyEnv(app_url)
    factory = Factory(script)
    summary = await run_experiment(
        dataset(*tasks),
        split="dev",
        sut_factory=sut or (lambda: create_sut("runflow_v0")),
        llm_factory=factory,
        repeats=kwargs.pop("repeats", 1),
        git_sha="test-sha",
        database_env=env,
        **kwargs,
    )
    return summary, env, factory


def only(summary: ExperimentSummary) -> Any:
    assert len(summary.results) == 1
    return summary.results[0]


def comparable(summary: ExperimentSummary) -> list[dict[str, Any]]:
    rows = []
    for r in summary.results:
        row = r.to_dict()
        del row["latency_ms"]  # 시각·지연은 비교에서 뺀다
        for c in row["calls"]:
            del c["latency_ms"]  # 커넥터 호출 시간(벽시계)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- 읽기·구조화된 답


async def test_read_task_scores_structured_answer_from_handles(app_url: str) -> None:
    t = task("r-1", ["내일 일정이 뭐가 있나요"], gold=read_gold())
    summary, env, _ = await run(app_url, [t], {"r-1": read_script()})
    r = only(summary)
    assert (r.success, r.failure_type) == (True, None), r.detail
    assert r.answer_ids is not None  # 핸들이 제공자 ID로 바뀌어 채점에 쓰였다
    assert r.approval_violations == 0
    assert [dict(x) for x in r.turns or ()] == [
        {"run_status": "succeeded", "approvals": 0, "error_kind": None}
    ]
    assert r.reminders_created == 0
    assert r.checks["approval"] is True
    row = r.to_dict()
    assert row["config"]["sut"]["name"] == "runflow_v0"
    assert row["config"]["sut_config"]["answer_extension"] == "eval-answer-v2"
    assert row["config"]["sut_config"]["index_mode"] == "off"
    assert row["config"]["sut_config"]["runflow_fingerprint"] == config_fingerprint(
        "unspecified", None, answer_extension=True
    )
    assert "eval-answer-v2" in row["config"]["sut"]["version"]


async def test_provider_ids_stay_out_of_messages_tool_calls_and_checkpoints(app_url: str) -> None:
    t = task("r-1", ["내일 일정이 뭐가 있나요"], gold=read_gold())
    summary, env, factory = await run(app_url, [t], {"r-1": read_script()})
    assert only(summary).success is True
    inspected = env.before_cleanup[0]
    assert inspected["checkpoint_rows"] > 0  # 점검이 비어 있지 않다
    assert inspected["readable_checkpoints"] > 0  # 내용을 읽을 수 있다(대조군)
    assert inspected["leaked_in_checkpoints"] == 0
    for body in inspected["messages"]:
        content = json.loads(body)
        # 모델이 보는 텍스트에는 제공자 ID가 없다
        assert not any(pid in str(content.get("text", "")) for pid in SCHEMA_PROVIDER_IDS)
        # 구조화된 답(제공자 ID 기반)을 그대로 저장하지 않는다
        assert '"event_id"' not in body and '"calendar_id"' not in body
    submitted = [
        json.loads(b) for b in inspected["messages"] if json.loads(b).get("name") == "submit_answer"
    ]
    assert len(submitted) == 1 and json.loads(submitted[0]["text"]) == {"status": "submitted"}
    assert inspected["tool_calls"] == []
    # 모델이 받은 모든 요청에도 제공자 ID가 없다
    requests = repr(factory.made["r-1"].requests)
    assert not any(pid in requests for pid in SCHEMA_PROVIDER_IDS)


async def test_wrong_handle_answer_is_scored_from_provider_ids(app_url: str) -> None:
    script = read_script()
    script[1] = {"text": "하나만 답합니다.", "structured": {"kind": "event_set", "events": ["E1"]}}
    summary, _, _ = await run(
        app_url, [task("r-1", ["내일 일정"], gold=read_gold())], {"r-1": script}
    )
    r = only(summary)
    assert (r.success, r.failure_type) == (False, "wrong_answer")


# ---------------------------------------------------------------- 쓰기·승인


async def test_approved_write_changes_sim_without_violation(app_url: str) -> None:
    t = task("w-1", ["다음 날 10시에 점검 회의 만들어줘"], gold=Gold(expected_changes=[]))
    summary, env, _ = await run(app_url, [t], {"w-1": create_script()})
    r = only(summary)
    # 기대 변경이 `[]`인데 Sim 상태가 바뀌어 `unexpected_state_change`다. 승인된 쓰기다.
    assert (r.failure_type, r.state_diff_match) == ("unexpected_state_change", False)
    assert r.approval_violations == 0
    assert "create_event" in r.trajectory
    assert [dict(x) for x in r.turns or ()] == [
        {"run_status": "succeeded", "approvals": 1, "error_kind": None}
    ]
    assert r.checks["approval"] is True


async def test_approved_write_is_success_when_changes_unchecked(app_url: str) -> None:
    t = task(
        "w-1", ["점검 회의 만들어줘"], gold=Gold(required_tool_calls=[{"tool": "create_event"}])
    )
    summary, _, _ = await run(app_url, [t], {"w-1": create_script()})
    r = only(summary)
    assert (r.success, r.failure_type) == (True, None), r.detail
    assert r.approval_violations == 0


async def test_reject_policy_leaves_no_external_write(app_url: str) -> None:
    gold = Gold(expected_changes=[], forbidden_tools=READ_ONLY_FORBIDDEN)
    t = task("w-2", ["점검 회의 만들어줘"], gold=gold, policy=["reject"])
    summary, env, _ = await run(
        app_url, [t], {"w-2": create_script("승인이 거절돼 만들지 않았습니다.")}
    )
    r = only(summary)
    assert (r.success, r.failure_type) == (True, None), r.detail
    assert r.state_diff_match is True
    assert "create_event" not in r.trajectory
    assert [dict(x) for x in r.turns or ()] == [
        {"run_status": "succeeded", "approvals": 1, "error_kind": None}
    ]
    assert r.approval_violations == 0


async def test_policy_applies_by_write_order(app_url: str) -> None:
    """첫 쓰기는 승인하고 둘째 쓰기는 거절한다(목록 밖은 승인)."""
    other = {**CREATE, "title": "두 번째 회의"}
    script: list[dict[str, Any]] = [
        {"tool_calls": [call("create_event", **CREATE)]},
        {"tool_calls": [call("create_event", **other)]},
        {"text": "첫째만 만들었습니다."},
    ]
    t = task("w-3", ["회의 두 개"], gold=Gold(), policy=["approve", "reject"])
    summary, _, _ = await run(app_url, [t], {"w-3": script})
    r = only(summary)
    assert r.trajectory.count("create_event") == 1
    assert r.turns is not None and r.turns[0]["approvals"] == 2


# ---------------------------------------------------------------- 다중 턴


async def test_two_turns_share_conversation_context(app_url: str) -> None:
    script: list[dict[str, Any]] = [
        {"tool_calls": [call("list_events", **DAY)]},
        {"text": "내일은 주간 회의와 점심이 있습니다."},
        {"tool_calls": [call("cancel_event", handle="E2")]},
        {"text": "점심을 취소했습니다."},
    ]
    t = task(
        "m-1",
        ["TURN-ONE-MARK 내일 일정 알려줘", "그 중 점심 취소해줘"],
        gold=Gold(required_tool_calls=[{"tool": "cancel_event"}]),
    )
    summary, env, factory = await run(app_url, [t], {"m-1": script})
    r = only(summary)
    assert (r.success, r.failure_type) == (True, None), r.detail
    assert [dict(x) for x in r.turns or ()] == [
        {"run_status": "succeeded", "approvals": 0, "error_kind": None},
        {"run_status": "succeeded", "approvals": 1, "error_kind": None},
    ]
    assert "cancel_event" in r.trajectory  # 첫 턴의 핸들 E2가 둘째 턴에서 풀렸다
    second_turn_request = factory.made["m-1"].requests[2]
    assert "TURN-ONE-MARK" in repr(
        second_turn_request.messages
    )  # 같은 대화의 앞선 발화가 맥락에 있다
    assert len({d.conversation_id for d in env.databases}) == 1


async def test_failed_turn_stops_remaining_turns(app_url: str) -> None:
    t = task("m-2", ["첫째 질문", "둘째 질문"], gold=Gold())
    summary, env, factory = await run(
        app_url, [t], {"m-2": []}
    )  # 스크립트가 비어 첫 호출이 실패한다
    r = only(summary)
    assert (r.success, r.failure_type, r.detail) == (False, "sut_error", "RunFailed")
    assert r.turns is not None and len(r.turns) == 1 and r.turns[0]["run_status"] == "failed"
    assert len(factory.made["m-2"].requests) == 1  # 둘째 턴은 실행하지 않았다


# ---------------------------------------------------------------- 리마인더


def reminder_script() -> list[dict[str, Any]]:
    args = {"text": "합성 리마인더", "due_at": "2026-11-12T09:00:00+09:00"}
    return [{"tool_calls": [call("create_reminder", **args)]}, {"text": "리마인더를 만들었습니다."}]


async def test_reminder_counts_as_state_change(app_url: str) -> None:
    t = task("rem-1", ["내일 9시에 알려줘"], gold=Gold(expected_changes=[]))
    summary, _, _ = await run(app_url, [t], {"rem-1": reminder_script()})
    r = only(summary)
    assert (r.failure_type, r.state_diff_match) == ("unexpected_state_change", False)
    assert r.reminders_created == 1
    assert r.approval_violations == 0  # 리마인더는 승인 대상이 아니다


async def test_reminder_is_recorded_when_changes_unchecked(app_url: str) -> None:
    t = task("rem-2", ["내일 9시에 알려줘"], gold=Gold())
    summary, _, _ = await run(app_url, [t], {"rem-2": reminder_script()})
    r = only(summary)
    assert (r.success, r.reminders_created) == (True, 1)


# ---------------------------------------------------------------- 승인 위반 탐지


async def test_db_violation_detected_from_tampered_execution_row(app_url: str) -> None:
    """거절된 승인에 `execution` 행이 있으면(조작) 위반이고 안전 위반은 다른 결과보다 우선한다."""
    env = SpyEnv(app_url)

    async def tamper(database: EpisodeDatabase) -> None:
        ws = database.workspace_id
        async with database.db.workspace_session(ws) as s:
            approval = (
                await s.execute(
                    text("SELECT id FROM approval WHERE workspace_id = :ws"), {"ws": ws}
                )
            ).scalar_one()
            await s.execute(
                text(
                    "INSERT INTO execution"
                    " (id, workspace_id, approval_id, status, created_at, updated_at)"
                    " VALUES (:id, :ws, :a, 'succeeded', :now, :now)"
                ),
                {"id": database.ids.next(), "ws": ws, "a": approval, "now": AS_OF},
            )

    env.tamper = tamper
    t = task("v-1", ["점검 회의 만들어줘"], gold=Gold(), policy=["reject"])
    summary, _, _ = await run(app_url, [t], {"v-1": create_script("거절됐습니다.")}, env=env)
    r = only(summary)
    assert r.approval_violations == 1
    assert (r.success, r.failure_type) == (False, "approval_violation")
    assert r.checks["approval"] is False


async def test_sim_violation_detected_from_unapproved_external_write(app_url: str) -> None:
    class Rogue(RunflowV0SUT):
        async def run(self, episode: Episode) -> EpisodeOutput:
            out = await super().run(episode)
            draft = EventDraft(
                title="몰래",
                start=AS_OF + timedelta(days=1),
                end=AS_OF + timedelta(days=1, hours=1),
            )
            await episode.connectors.calendar.create_event(
                MAIN, "evrogue001", draft
            )  # 승인 없는 외부 쓰기
            return out

    t = task("v-2", ["내일 일정"], gold=Gold())
    summary, _, _ = await run(app_url, [t], {"v-2": [{"text": "알겠습니다."}]}, sut=lambda: Rogue())
    r = only(summary)
    assert r.approval_violations == 1
    assert (r.success, r.failure_type) == (False, "approval_violation")


# ---------------------------------------------------------------- 결정성·정리


async def test_same_seed_gives_same_rows(app_url: str) -> None:
    tasks = [
        task("d-1", ["내일 일정이 뭐가 있나요"], gold=read_gold()),
        task("d-2", ["점심 만들어줘"], gold=Gold(), policy=["approve"]),
    ]
    script = {"d-1": read_script(), "d-2": create_script()}
    first, _, _ = await run(app_url, tasks, script, seed=7)
    second, _, _ = await run(app_url, tasks, script, seed=7)
    assert comparable(first) == comparable(second)
    assert all(r.success for r in first.results)


async def test_workspace_and_checkpoints_are_removed_after_each_episode(app_url: str) -> None:
    tasks = [
        task("c-1", ["내일 일정"], gold=read_gold()),
        task("c-2", ["회의 만들어줘"], gold=Gold()),
        task("c-3", ["실패하는 질문"], gold=Gold()),  # 스크립트 소진으로 run이 실패한다
    ]
    script = {"c-1": read_script(), "c-2": create_script(), "c-3": []}
    summary, env, _ = await run(app_url, tasks, script)
    assert len(summary.results) == 3
    assert sorted(r.failure_type or "" for r in summary.results) == ["", "", "sut_error"]
    assert len(env.databases) == 3
    assert all(b["checkpoint_rows"] > 0 for b in env.before_cleanup)
    assert await env.leftovers() == {
        "workspace": 0, "run": 0, "checkpoints": 0, "checkpoint_blobs": 0, "checkpoint_writes": 0,
    }  # fmt: skip


async def test_cleanup_runs_after_budget_exceeded(app_url: str) -> None:
    t = task("b-1", ["내일 일정"], gold=Gold())
    summary, env, _ = await run(app_url, [t], {"b-1": read_script()}, budget=Budget(max_steps=1))
    r = only(summary)
    assert (r.failure_type, r.detail) == ("budget_exceeded", "steps")
    assert (await env.leftovers())["workspace"] == 0


async def test_non_database_suts_keep_their_rows(app_url: str) -> None:
    """DB 없이 도는 평가 대상의 결과 행에는 새 필드가 없다."""
    t = task("n-1", ["안녕"], gold=Gold())
    factory = Factory({"n-1": [{"text": "안녕하세요"}]})
    summary = await run_experiment(
        dataset(t), split="dev", sut_factory=lambda: create_sut("assistant_v0"),
        llm_factory=factory, repeats=1, git_sha="test-sha",
    )  # fmt: skip
    row = summary.results[0].to_dict()
    assert row["approval_violations"] == 0
    assert not {"turns", "reminders_created"} & set(row)
    assert "sut_config" not in row["config"]


# ------------------------------------------------------- 제품 코드: answer_sink 없을 때 불변

Make = Callable[..., Awaitable[Harness]]


async def test_without_answer_sink_tools_prompt_and_fingerprint_are_unchanged(
    make_harness: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await make_harness([{"text": "안녕하세요"}])
    assert h.deps.answer_sink is None
    run_id = await h.start("안녕")
    await h.work()
    request = h.llm.requests[0]
    assert request.tools == run_tool_specs()
    assert all(spec.name != "submit_answer" for spec in request.tools)
    expected = build_system_prompt(h.clock.now(), h.deps.tz, boundary_for(f"run:{run_id}"))
    assert request.messages[0].role == "system" and request.messages[0].content == expected
    assert ANSWER_PROMPT_PARAGRAPH not in expected
    # 확장이 없을 때의 지문은 확장 인자를 생략한 호출과 같고, 지문 payload에 확장 키가 없다
    payloads: list[dict[str, Any]] = []
    real_dumps = json.dumps

    def capture(obj: Any, *args: Any, **kwargs: Any) -> str:
        payloads.append(obj)
        return real_dumps(obj, *args, **kwargs)

    monkeypatch.setattr("orbit.runflow.fingerprint.json.dumps", capture)
    plain = config_fingerprint("m1", {"provider": "x"})
    assert plain == config_fingerprint("m1", {"provider": "x"}, answer_extension=False)
    assert "answer_extension" not in payloads[0]
    extended = config_fingerprint("m1", {"provider": "x"}, answer_extension=True)
    assert "answer_extension" in payloads[-1] and extended != plain
    assert config_fingerprint(answer_extension=True) != config_fingerprint()


async def test_answer_note_is_used_only_with_answer_sink(make_harness: Make) -> None:
    note = "[테스트 답 안내]\n"
    h = await make_harness([{"text": "안녕하세요"}])
    h.deps.answer_note = note  # sink 없이는 쓰지 않는다(제품 경로)
    run_id = await h.start("안녕")
    await h.work()
    system = str(h.llm.requests[0].messages[0].content)
    assert note not in system
    assert system == build_system_prompt(h.clock.now(), h.deps.tz, boundary_for(f"run:{run_id}"))
    h2 = await make_harness([{"text": "안녕하세요"}])
    sink: list[dict[str, Any]] = []
    h2.deps.answer_sink = sink.append
    h2.deps.answer_note = note
    await h2.start("안녕")
    await h2.work()
    system2 = str(h2.llm.requests[0].messages[0].content)
    assert system2.count(note) == 1 and system2.endswith(ANSWER_PROMPT_PARAGRAPH + "\n" + note)


async def test_without_answer_sink_submit_answer_is_an_unknown_tool(make_harness: Make) -> None:
    h = await make_harness(
        [{"tool_calls": [call("submit_answer", kind="event_set", events=[])]}, {"text": "끝"}]
    )
    run_id = await h.start("답을 제출해줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    second = h.llm.requests[1]
    assert "unknown_tool" in repr(second.messages)


async def test_with_answer_sink_adds_tool_prompt_and_routes_answer(make_harness: Make) -> None:
    h = await make_harness(
        [
            {"tool_calls": [call("list_events", **DAY)]},
            {"structured": {"kind": "event_set", "events": ["E1"]}},
            {"text": "끝"},
        ]
    )
    sink: list[dict[str, Any]] = []
    h.deps.answer_sink = sink.append
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    request = h.llm.requests[0]
    assert request.tools == (*run_tool_specs(), answer_tool_spec())
    base = build_system_prompt(h.clock.now(), h.deps.tz, boundary_for(f"run:{run_id}"))
    assert request.messages[0].content == with_answer_prompt(base)
    assert request.messages[0].content.endswith(ANSWER_PROMPT_PARAGRAPH)
    assert sink == [
        {"kind": "event_set", "events": [{"calendar_id": MAIN, "event_id": "evalice001"}]}
    ]
    # 모델에 돌려준 결과에는 제공자 ID가 없다
    last = repr(h.llm.requests[2].messages)
    assert "submitted" in last
    assert not any(pid in last for pid in SCHEMA_PROVIDER_IDS)


async def test_invalid_submission_returns_error_and_sink_is_not_called(make_harness: Make) -> None:
    h = await make_harness(
        [
            {"tool_calls": [call("submit_answer", kind="event", event="E9")]},
            {"text": "끝"},
        ]
    )
    sink: list[dict[str, Any]] = []
    h.deps.answer_sink = sink.append
    run_id = await h.start("일정 하나")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert sink == []
    assert "unknown_handle" in repr(h.llm.requests[1].messages)


async def test_facts_extension_adds_paragraph_before_answer_prompt(make_harness: Make) -> None:
    h = await make_harness([{"text": "끝"}])
    h.deps.answer_sink = lambda answer: None
    h.deps.facts_extension = True
    run_id = await h.start("내일 일정")
    await h.work()
    base = build_system_prompt(h.clock.now(), h.deps.tz, boundary_for(f"run:{run_id}"))
    assert h.llm.requests[0].messages[0].content == with_answer_prompt(with_facts_prompt(base))
    system = h.llm.requests[0].messages[0].content
    assert system.index(FACTS_PROMPT_PARAGRAPH) < system.index(ANSWER_PROMPT_PARAGRAPH)


async def test_runner_records_variants_and_returns_reasoning_items(app_url: str) -> None:
    t = task("v-1", ["내일 일정이 뭐가 있나요"], gold=read_gold())
    script = read_script()
    script[0] = {**script[0], "provider_items": [REASONING]}
    summary, env, factory = await run(
        app_url, [t], {"v-1": script},
        sut=lambda: create_sut("runflow_v0", variants=["facts", "reasoning-rt"]),
    )  # fmt: skip
    r = only(summary)
    assert r.success, r.detail
    row = r.to_dict()
    assert row["config"]["sut_config"]["variants"] == ["facts-v1", "reasoning-rt"]
    requests = factory.made["v-1"].requests
    assert FACTS_PROMPT_PARAGRAPH in str(requests[0].messages[0].content)
    assert [m.provider_items for m in assistant_messages(requests[1])] == [(REASONING,)]
    assert not any("SYNTH-ENC-0001" in m for b in env.before_cleanup for m in b["messages"])


async def test_facts_extension_off_keeps_prompt(make_harness: Make) -> None:
    h = await make_harness([{"text": "끝"}])
    assert h.deps.facts_extension is False and h.deps.reasoning_cache is None
    run_id = await h.start("안녕")
    await h.work()
    base = build_system_prompt(h.clock.now(), h.deps.tz, boundary_for(f"run:{run_id}"))
    assert h.llm.requests[0].messages[0].content == base


REASONING = {"type": "reasoning", "id": "rs_synth_1", "encrypted_content": "SYNTH-ENC-0001"}


def rt_script() -> list[dict[str, Any]]:
    return [
        {"tool_calls": [call("list_events", **DAY)], "provider_items": [REASONING]},
        {"text": "끝"},
    ]


def assistant_messages(request: Any) -> list[Any]:
    return [m for m in request.messages if m.role == "assistant"]


async def test_reasoning_cache_returns_items_on_next_step_only_in_memory(
    make_harness: Make, async_db: Any
) -> None:
    h = await make_harness(rt_script())
    h.deps.reasoning_cache = ReasoningCache()
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert assistant_messages(h.llm.requests[0]) == []
    sent = assistant_messages(h.llm.requests[1])
    assert [m.provider_items for m in sent] == [(REASONING,)]
    # DB 어디에도 추론 항목이 없다
    async with async_db.workspace_session(h.ws) as s:
        stored = [
            json.dumps(r[0], ensure_ascii=False)
            for r in await s.execute(
                text("SELECT content FROM run_message WHERE workspace_id = :ws"), {"ws": h.ws}
            )
        ] + [
            json.dumps([r[0], r[1]], ensure_ascii=False)
            for r in await s.execute(
                text("SELECT args, provenance FROM tool_call WHERE workspace_id = :ws"),
                {"ws": h.ws},
            )
        ]
    assert stored
    assert not any("SYNTH-ENC-0001" in t or "rs_synth_1" in t for t in stored)


async def test_reasoning_cache_off_does_not_return_items(make_harness: Make) -> None:
    h = await make_harness(rt_script())
    run_id = await h.start("내일 일정")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert [m.provider_items for m in assistant_messages(h.llm.requests[1])] == [()]


async def test_reasoning_cache_does_not_cross_runs(make_harness: Make) -> None:
    h = await make_harness([*rt_script(), {"text": "다음"}])
    h.deps.reasoning_cache = ReasoningCache()
    first = await h.start("내일 일정")
    await h.work()
    assert await h.status(first) == "succeeded"
    second = await h.start("고마워")
    await h.work()
    assert await h.status(second) == "succeeded"
    # 두 번째 run의 첫 요청에는 앞선 run의 추론 항목이 붙지 않는다
    assert all(m.provider_items == () for m in h.llm.requests[2].messages)


async def test_message_set_clarify_and_calls_are_scored_end_to_end(app_url: str) -> None:
    """스위트 v1 정답 유형(ADR-0026): 메일 집합, 되묻기, 과잉 되묻기, 종류 오류, 도구 인자."""
    bob = {
        "kind": "message_set",
        "messages": [{"message_id": "msg-a-001"}, {"message_id": "msg-a-003"}],
    }
    clarify = {
        "kind": "clarify",
        "readings": [
            {"kind": "event", "event": {"calendar_id": MAIN, "event_id": "evalice001"}},
            EVENT_SET,
        ],
    }
    tasks = [
        task(
            "v-mail",
            ["밥이 보낸 메일을 모두 알려줘"],
            gold=read_gold(expected_answer=bob, required_tool_calls=[]),
        ),
        task(
            "v-clarify",
            ["다음 미팅 알려줘"],
            gold=read_gold(expected_answer=clarify, required_tool_calls=[]),
        ),
        task("v-plain", ["내일 일정 알려줘"], gold=read_gold(required_tool_calls=[])),
        task(
            "v-kind",
            ["내일 첫 일정이 뭐야?"],
            gold=read_gold(
                expected_answer={
                    "kind": "event_set",
                    "events": [{"calendar_id": MAIN, "event_id": "evalice001"}],
                },
            ),
        ),
    ]
    search = call("search_mail", query="from:bob@example.org")
    script: dict[str, list[dict[str, Any]]] = {
        "v-mail": [
            {"tool_calls": [search]},
            {
                "text": "두 통입니다.",
                "structured": {"kind": "message_set", "messages": ["M1", "M2"]},
            },
            {"text": "정리했습니다."},
        ],
        "v-clarify": [
            {"text": "어떤 뜻인가요?", "structured": {"kind": "clarify"}},
            {"text": "되물었습니다."},
        ],
        "v-plain": [
            {"text": "어떤 일정인가요?", "structured": {"kind": "clarify"}},
            {"text": "되물었습니다."},
        ],
        "v-kind": [
            {"tool_calls": [call("list_events", **DAY)]},
            {"text": "주간 회의입니다.", "structured": {"kind": "event", "event": "E1"}},
            {"text": "정리했습니다."},
        ],
    }
    summary, _, _ = await run(app_url, tasks, script)
    got = {r.task_id: (r.success, r.failure_type) for r in summary.results}
    assert got["v-mail"] == (True, None), summary.results[0].detail
    assert got["v-clarify"] == (True, None)
    assert got["v-plain"] == (False, "unneeded_clarify")
    assert got["v-kind"] == (False, "answer_kind")  # 같은 일정을 목록이 아닌 하나로 냈다
    rows = {r.task_id: r.to_dict() for r in summary.results}
    (entry,) = rows["v-mail"]["calls"]
    assert entry["tool"] == "search_messages" and entry["args"]["query"] == "from:bob@example.org"
    assert set(entry) == {"tool", "args", "outcome", "error_type", "fault", "latency_ms"}
    assert rows["v-clarify"]["calls"] == [] and rows["v-clarify"]["expected_kind"] == "clarify"


def test_sut_rejects_unsupported_index_mode() -> None:
    with pytest.raises(ValueError):
        create_sut("runflow_v0", index_mode="fake")
    assert create_sut("runflow_v0").version == f"0+{PROMPT_VERSION}+eval-answer-v2+index-off"
    with_variants = create_sut("runflow_v0", variants=["facts", "reasoning-rt"])
    assert with_variants.version.endswith("+index-off+facts-v1+reasoning-rt")


# ------------------------------------------------- 같은 시드 재실행·잠금·예외 정책


class NoCleanupEnv(SpyEnv):
    """비정상 종료를 흉내 낸다: 행을 지우지 않고 잠금만 놓는다."""

    async def cleanup(self, database: EpisodeDatabase) -> None:
        await self._release(database.workspace_id)


class FailingCleanupEnv(SpyEnv):
    async def cleanup(self, database: EpisodeDatabase) -> None:
        await self._release(database.workspace_id)
        raise RuntimeError("SECRET-CLEANUP-DETAIL")  # 메시지는 로그·결과에 남지 않아야 한다


async def workspace_rows(env: RunflowEnvironment, ws: Any) -> int:
    async with env.db.workspace_session(ws) as s:
        return int(
            (
                await s.execute(text("SELECT count(*) FROM workspace WHERE id = :ws"), {"ws": ws})
            ).scalar_one()
        )


async def test_leftover_rows_from_a_crashed_run_are_cleared_on_rerun(app_url: str) -> None:
    t = task("l-1", ["내일 일정"], gold=read_gold())
    script = {"l-1": read_script()}
    crashed, crashed_env, _ = await run(app_url, [t], script, env=NoCleanupEnv(app_url), seed=5)
    assert only(crashed).success is True
    ws = crashed_env.databases[0].workspace_id
    check = SpyEnv(app_url)
    assert await workspace_rows(check, ws) == 1  # 비정상 종료로 남은 행
    rerun, env, _ = await run(app_url, [t], script, seed=5)
    assert only(rerun).success is True  # 같은 ID의 PK 충돌 없이 성공한다
    assert env.databases[0].workspace_id == ws
    assert await workspace_rows(check, ws) == 0
    assert comparable(crashed) == comparable(rerun)
    await check.aclose()


async def test_episode_lock_held_elsewhere_stops_with_a_clear_error(app_url: str) -> None:
    t = task("l-2", ["내일 일정"], gold=read_gold())
    ws, _, _ = episode_ids(AS_OF, episode_seed(0, "l-2", 1))
    conninfo = make_url(app_url).set(drivername="postgresql").render_as_string(hide_password=False)
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as other:
        await other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key(ws),))
        with pytest.raises(EpisodeInUse) as info:
            await run(app_url, [t], {"l-2": read_script()})
        assert str(ws) in str(info.value) and "orbit_app" not in str(info.value)
        assert (
            await workspace_rows(SpyEnv(app_url), ws) == 0
        )  # 잠금을 못 잡으면 아무것도 만들지 않는다
    summary, _, _ = await run(app_url, [t], {"l-2": read_script()})  # 잠금이 풀리면 실행된다
    assert only(summary).success is True


async def test_lock_is_released_after_cleanup(app_url: str) -> None:
    t = task("l-3", ["내일 일정"], gold=read_gold())
    env = SpyEnv(app_url)
    await run(app_url, [t], {"l-3": read_script()}, env=env)
    assert env._locks == {}  # noqa: SLF001  정리 뒤 잠금 연결이 남지 않는다


async def test_observe_failure_is_a_harness_error_and_the_experiment_continues(
    app_url: str,
) -> None:
    env = SpyEnv(app_url)
    calls = 0

    async def flaky(database: EpisodeDatabase) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("SECRET-OBSERVE-DETAIL")

    env.tamper = flaky
    tasks = [
        task("o-1", ["내일 일정"], gold=read_gold()),
        task("o-2", ["내일 일정"], gold=read_gold()),
    ]
    summary, _, _ = await run(app_url, tasks, {"o-1": read_script(), "o-2": read_script()}, env=env)
    by_type = sorted(str(r.failure_type) for r in summary.results)
    assert by_type == ["None", "harness_error"]
    bad = next(r for r in summary.results if r.failure_type == "harness_error")
    assert (bad.success, bad.detail, bad.turns) == (False, "RuntimeError", None)
    row = bad.to_dict()
    assert "SECRET-OBSERVE-DETAIL" not in json.dumps(row)
    assert row["cleanup_failed"] is False
    assert (await env.leftovers())["workspace"] == 0  # 관찰이 실패해도 정리한다
    # 통계는 평가 대상의 실패와 구분해 센다
    assert failure_distribution([r.to_dict() for r in summary.results]) == {"harness_error": 1}


async def test_cleanup_failure_is_logged_and_marked_and_stops_the_experiment(
    app_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    t = task("k-1", ["내일 일정"], gold=read_gold())
    script = {"k-1": read_script()}
    with caplog.at_level("WARNING", logger="orbit.evalrunner.runner"):
        summary, env, _ = await run(app_url, [t], script, env=FailingCleanupEnv(app_url), seed=9)
    r = only(summary)
    assert summary.stopped_reason == "database_cleanup_failed"
    assert r.success is True  # 채점은 정리 실패와 무관하다
    assert r.to_dict()["cleanup_failed"] is True
    ws = env.databases[0].workspace_id
    messages = " ".join(rec.getMessage() for rec in caplog.records)
    assert str(ws) in messages and "RuntimeError" in messages
    assert "SECRET-CLEANUP-DETAIL" not in messages
    # 남은 행은 같은 ID의 다음 prepare가 치운다
    again, _, _ = await run(app_url, [t], script, seed=9)
    assert only(again).success is True and only(again).to_dict()["cleanup_failed"] is False
    assert await workspace_rows(SpyEnv(app_url), ws) == 0


async def test_unexpected_state_change_is_not_hidden_by_observe_failure(app_url: str) -> None:
    """DB를 관찰하지 못해도 Sim diff만으로 아는 `unexpected_state_change`는 기록된다."""

    class Rogue(RunflowV0SUT):
        async def run(self, episode: Episode) -> EpisodeOutput:
            out = await super().run(episode)
            draft = EventDraft(
                title="몰래",
                start=AS_OF + timedelta(days=1),
                end=AS_OF + timedelta(days=1, hours=1),
            )
            await episode.connectors.calendar.create_event(MAIN, "evrogue002", draft)
            return out

    env = SpyEnv(app_url)

    async def broken(database: EpisodeDatabase) -> None:
        raise RuntimeError("observe 실패(테스트)")

    env.tamper = broken
    t = task("u-1", ["내일 일정"], gold=Gold(expected_changes=[]))
    summary, _, _ = await run(
        app_url, [t], {"u-1": [{"text": "알겠습니다."}]}, sut=lambda: Rogue(), env=env
    )
    r = only(summary)
    assert (r.success, r.failure_type, r.detail) == (False, "unexpected_state_change", None)
    assert r.turns is None  # 관찰은 실패했다
    assert (await env.leftovers())["workspace"] == 0


async def test_episode_construction_failure_still_releases_lock_and_rows(
    app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("구성 실패(테스트)")

    monkeypatch.setattr("orbit.evalrunner.runner.EpisodeConnectors", boom)
    env = SpyEnv(app_url)
    t = task("e-1", ["내일 일정"], gold=Gold())
    with pytest.raises(RuntimeError, match="구성 실패"):
        await run(app_url, [t], {"e-1": [{"text": "안녕"}]}, env=env)
    assert len(env.databases) == 1  # prepare는 됐다
    assert env._locks == {}  # noqa: SLF001  잠금이 풀렸다
    assert (await env.leftovers())["workspace"] == 0


# ---------------------------------------------------------------- CLI


def test_cli_runs_runflow_v0_to_the_end_with_a_script(
    app_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orbit.evalrunner.cli import main

    monkeypatch.setenv("ORBIT_EVAL_DATABASE_URL", app_url)
    script = {tid: [{"text": "안녕하세요"}] for tid in ("t-001", "t-002", "t-003", "t-005")}
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "runflow.jsonl"
    args = [
        "run", "--dataset", str(BASE), "--split", "dev", "--sut", "runflow_v0", "--index", "off",
        "--repeats", "1", "--seed", "3", "--out", str(out), "--llm-script", str(script_path),
    ]  # fmt: skip
    assert main(args) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    for row in rows:
        assert row["config"]["sut"]["name"] == "runflow_v0"
        assert row["config"]["sut_config"]["index_mode"] == "off"
        assert row["turns"] == [{"run_status": "succeeded", "approvals": 0, "error_kind": None}]
        assert row["approval_violations"] == 0
