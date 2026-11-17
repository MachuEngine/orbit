"""실제 테스트 DB·그래프에서 분류/act 대기와 범주 밖 반복을 검증한다(ADR-0034)."""

import asyncio
from typing import Any

import pytest

from integration.test_runflow_eval import only, read_gold, run, task
from orbit.evalrunner.episode import Budget
from orbit.evalrunner.suts import create_sut
from orbit.llm.scripted import ScriptedLLM
from orbit.llm.types import LLMRequest, LLMResponse
from orbit.runflow.patterns import CATEGORIES


@pytest.mark.parametrize("kind", ["classify", "act"])
async def test_fixed_workflow_llm_wait_obeys_episode_deadline(
    app_url: str, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original = ScriptedLLM.complete
    seen: list[str | None] = []

    async def complete(llm: ScriptedLLM, request: LLMRequest) -> LLMResponse:
        seen.append(request.metadata.get("step_kind"))
        if request.metadata.get("step_kind") == kind:
            await asyncio.Event().wait()  # 모델/네트워크 대기의 취소 가능한 대역. 외부 호출 없음.
        return await original(llm, request)

    monkeypatch.setattr(ScriptedLLM, "complete", complete)
    t = task(f"deadline-{kind}", ["내일 일정을 알려줘"], gold=read_gold())
    script: list[dict[str, Any]] = [{"text": '{"category": "일정 조회"}'}, {"text": "끝"}]
    async with asyncio.timeout(5):
        summary, env, _ = await run(
            app_url,
            [t],
            {t.task_id: script},
            sut=lambda: create_sut("runflow_v0", pattern="fixed_workflow"),
            budget=Budget(max_seconds=0.3),
        )
    result = only(summary)
    assert kind in seen  # DB 준비 시간이 아니라 의도한 모델 경계에서 대기했다.
    assert (result.failure_type, result.detail) == ("budget_exceeded", "time")
    assert result.cost_usd is None  # 취소된 진행 중 LLM 호출은 비용 미상이다.
    assert result.llm_calls_by_kind is not None and result.llm_calls_by_kind[kind] == 1
    assert result.cleanup_failed is False
    assert result.turns is None and result.reminders_created is None
    assert "approval" not in result.checks
    assert not env._locks  # noqa: SLF001  취소 뒤 세션 잠금도 해제했다.
    assert all(n == 0 for n in (await env.leftovers()).values())


async def test_disallowed_tools_cannot_keep_fixed_workflow_running(app_url: str) -> None:
    cap = CATEGORIES["일정 조회"].max_steps
    assert cap is not None
    t = task("deadline-forbidden-loop", ["내일 일정 알려줘"], gold=read_gold())
    script: list[dict[str, Any]] = [
        {"text": '{"category": "일정 조회"}'},
        *[
            {
                "tool_calls": [
                    {
                        "name": "send_mail",
                        "arguments": {"to": ["synthetic@example.net"], "subject": "s", "body": "b"},
                    }
                ]
            }
            for _ in range(cap)
        ],
    ]
    async with asyncio.timeout(5):
        summary, env, factory = await run(
            app_url,
            [t],
            {t.task_id: script},
            sut=lambda: create_sut("runflow_v0", pattern="fixed_workflow"),
        )
    result = only(summary)
    assert result.success is False and result.detail == "RunFailed"
    assert result.turns is not None and result.turns[0]["error_kind"] == "max_steps"
    assert result.llm_calls_by_kind == {"classify": 1, "act": cap}
    assert result.trajectory == () and result.approval_violations == 0
    assert result.turns[0]["approvals"] == 0
    requests = factory.made[t.task_id].requests
    assert "tool_not_allowed" in repr(requests[-1].messages)
    assert all(n == 0 for n in (await env.leftovers()).values())
