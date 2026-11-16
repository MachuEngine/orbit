"""평가 대상 `runflow_v0`: 실사용 그래프(`orbit.runflow`)를 워커 없이 직접 구동한다.

근거: ADR-0023 §1·§3·§5.

러너가 준비한 평가 DB 핸들(`Episode.database`)로, 사용자 발화마다 run을 만들고(API가 하는
기록과 같은 records 함수, 작업 큐 등록 없음) `advance_run`을 반복한다. `waiting_approval`이면 과제의
`approval_policy`대로 시뮬레이션 사용자의 결정을 기록하고 다시 구동한다. 앞 run이 끝난 뒤 같은
대화에서 다음 run을 만든다. 구조화된 답은 `RunDeps.answer_sink`(메모리)로만 받고, 마지막 run에서
제출한 답을 `EpisodeOutput.structured`로 낸다.

궤적·상태·승인 위반·사용량은 이 클래스가 보고하지 않는다. 러너가 Sim 호출 기록, DB,
`UsageRecorder`에서 직접 읽는다(ADR-0009 §3).
"""

import logging
import uuid
from collections.abc import Callable, Iterable
from typing import Any
from zoneinfo import ZoneInfo

from orbit.core.clock import FixedClock
from orbit.evalrunner.episode import (
    Episode,
    EpisodeDatabase,
    EpisodeOutput,
    SutTurn,
)
from orbit.evalrunner.runflow_env import EpisodeResolver
from orbit.evalrunner.runflow_index import EvalIndexProvider
from orbit.evalrunner.trace import EpisodeTrace
from orbit.llm.interface import LLM
from orbit.llm.types import LLMRequest, LLMResponse
from orbit.mailindex.deps import MailIndexDeps
from orbit.records.approvals import decide_approval
from orbit.records.runs import (
    TERMINAL_RUN_STATUSES,
    append_message,
    create_run,
    get_run,
    list_messages,
)
from orbit.runflow.answer import ANSWER_EXTENSION_VERSION
from orbit.runflow.deps import RunDeps
from orbit.runflow.eval_variants import (
    FACTS_EXTENSION_VERSION,
    REASONING_ROUNDTRIP_VERSION,
    ReasoningCache,
)
from orbit.runflow.fingerprint import PATTERN, config_fingerprint
from orbit.runflow.graph import advance_run
from orbit.runflow.patterns import PATTERN_VERSIONS, PATTERNS, REACT, PatternMemory
from orbit.runflow.prompt import PROMPT_VERSION
from orbit.runflow.repo import latest_approval_for_run, lock_run

logger = logging.getLogger(__name__)

MAX_APPROVALS_PER_RUN = (
    5  # run 하나의 승인 왕복 상한(그래프 `max_steps`와 별개로 무한 반복을 막는다)
)
MAX_DRIVE_ROUNDS = 2 * (MAX_APPROVALS_PER_RUN + 1)  # `advance_run` 호출 상한(run 하나)
INDEX_MODES = ("off", "on")
# CLI 이름(`--runflow-variant`) -> SUT 버전 문자열에 붙는 이름 (ADR-0023 §10)
VARIANTS = {"facts": FACTS_EXTENSION_VERSION, "reasoning-rt": REASONING_ROUNDTRIP_VERSION}


class RunFailed(RuntimeError):  # noqa: N818  도메인 용어
    """run이 실패·취소로 끝났다. 남은 턴은 실행하지 않는다. 원인 종류는 DB의 `run.error_kind`다."""


class ApprovalLimitExceeded(RuntimeError):  # noqa: N818
    """run 하나에서 승인 왕복이 상한을 넘었다."""


class RunNotSettled(RuntimeError):  # noqa: N818
    """`advance_run`을 상한만큼 불렀는데 run이 승인 대기도 종료도 아니다."""


class _StepGuardLLM:
    """LLM을 부르기 전에 단계 한도를 검사한다. 그래프가 LLM을 호출하는 지점이 단계 경계다."""

    def __init__(self, inner: LLM, consume: Callable[[], None]) -> None:
        self._inner = inner
        self._consume = consume

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self._consume()
        return await self._inner.complete(request)


class _TraceLLM:
    """단계 종류(`metadata["step_kind"]`)가 붙은 호출을 실행 기록에 남긴다(ADR-0030)."""

    def __init__(self, inner: LLM, trace: EpisodeTrace) -> None:
        self._inner = inner
        self._trace = trace

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self._inner.complete(request)
        kind = request.metadata.get("step_kind")
        if kind:
            self._trace.record_pattern_step(str(kind), response.text)
        return response


class _IndexWatchLLM:
    """`--index on`에서 색인 메일이 있는데 검색이 0건이었던 횟수를 센다(경고용, 채점에 안 쓴다).

    모델에 들어가는 도구 결과(데이터 봉투)를 호출 ID별로 한 번씩만 본다.
    """

    def __init__(self, inner: LLM, index: EvalIndexProvider, account_id: str) -> None:
        self._inner = inner
        self._index = index
        self._account = account_id
        self._seen: set[str] = set()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        for m in request.messages:
            if m.role != "tool" or m.tool_call_id in self._seen:
                continue
            self._seen.add(str(m.tool_call_id))
            body = m.content or ""
            if (
                'source="search_indexed_mail"' in body
                and '"results": []' in body
                and "index_empty" not in body
                and self._index.has_mails(self._account)
            ):
                self._index.zero_hit_searches += 1
                logger.warning("색인 메일이 있는데 검색 결과가 0건이다: account=%s", self._account)
        return await self._inner.complete(request)


class _Submission:
    """`answer_sink`. 마지막 제출이 유효하다. 턴이 바뀌면 비운다."""

    def __init__(self) -> None:
        self.answer: dict[str, Any] | None = None

    def __call__(self, answer: dict[str, Any]) -> None:
        self.answer = answer


def decision_for(policy: tuple[str, ...], index: int) -> bool:
    """n번째(0부터) 승인 요청의 결정. 정책 밖은 승인이다(ADR-0023 §3)."""
    return index >= len(policy) or policy[index] != "reject"


class RunflowV0SUT:
    name = "runflow_v0"

    def __init__(
        self,
        *,
        index_mode: str = "off",
        variants: Iterable[str] = (),
        pattern: str = REACT,
        index: EvalIndexProvider | None = None,
    ) -> None:
        if index_mode not in INDEX_MODES:
            raise ValueError(f"지원하지 않는 색인 모드: {index_mode!r} (지원: {INDEX_MODES})")
        if (index_mode == "on") != (index is not None):
            raise ValueError("--index on에는 색인 제공자가 필요하고, off에는 쓰지 않는다")
        self._index = index
        chosen = set(variants)
        unknown = chosen - set(VARIANTS)
        if unknown:
            raise ValueError(f"지원하지 않는 변형: {sorted(unknown)} (지원: {sorted(VARIANTS)})")
        if pattern not in PATTERNS:
            raise ValueError(f"지원하지 않는 패턴: {pattern!r} (지원: {PATTERNS})")
        self._pattern = pattern
        self._index_mode = index_mode
        self._facts = "facts" in chosen
        self._reasoning_rt = "reasoning-rt" in chosen

    @property
    def variant_names(self) -> list[str]:
        """켠 변형의 버전 이름(정렬)."""
        return sorted(
            name
            for key, name in VARIANTS.items()
            if (key == "facts" and self._facts) or (key == "reasoning-rt" and self._reasoning_rt)
        )

    @property
    def version(self) -> str:
        base = f"0+{PROMPT_VERSION}+{ANSWER_EXTENSION_VERSION}+index-{self._index_mode}"
        # 켠 변형은 고정 순서(facts-v1, reasoning-rt)로 붙인다
        ordered = [
            n
            for n in (FACTS_EXTENSION_VERSION, REASONING_ROUNDTRIP_VERSION)
            if n in self.variant_names
        ]
        extra = [] if self._pattern == REACT else [PATTERN_VERSIONS[self._pattern]]
        return "".join([base, *(f"+{n}" for n in ordered), *(f"+{n}" for n in extra)])

    def config_info(
        self, *, model_id: str, llm_info: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """결과의 설정 지문에 더할 항목(runflow 지문, 답 제출 확장, 색인 모드)."""
        info: dict[str, Any] = {
            "runflow_fingerprint": config_fingerprint(
                model_id,
                llm_info,
                answer_extension=True,
                facts_extension=self._facts,
                reasoning_roundtrip=self._reasoning_rt,
                agent_pattern=self._pattern,
            ),
            "answer_extension": ANSWER_EXTENSION_VERSION,
            "variants": self.variant_names,
            "index_mode": self._index_mode,
            "max_approvals_per_run": MAX_APPROVALS_PER_RUN,
        }
        if self._index is not None:  # off는 키를 더하지 않는다(기존 결과와 같게 둔다)
            info["index"] = self._index.config_info()
        if self._pattern != REACT:  # react는 키를 더하지 않는다(기존 결과와 같게 둔다)
            info["pattern"] = self._pattern
            info["pattern_version"] = PATTERN_VERSIONS[self._pattern]
        return info

    async def run(self, episode: Episode) -> EpisodeOutput:
        database = episode.database
        if database is None:
            raise RuntimeError("runflow_v0에는 러너가 준비한 평가 DB 핸들이 필요하다")
        submission = _Submission()
        memory = PatternMemory() if self._pattern != REACT else None
        clock = FixedClock(episode.task.as_of_time)
        resolver = EpisodeResolver(
            calendar=episode.connectors.calendar,
            mail=episode.connectors.mail,
            drive=_require_drive(episode),
        )
        deps = RunDeps(
            db=database.db,
            clock=clock,
            ids=database.ids,
            llm=self._watch(
                _TraceLLM(_StepGuardLLM(episode.llm, episode.steps.consume), episode.trace),
                episode,
            ),
            resolver=resolver,
            tz=ZoneInfo(episode.task.time_zone),
            checkpoint_conninfo=database.checkpoint_conninfo,
            jobs=None,
            mail_index=self._mail_index(episode, database, clock),
            answer_sink=submission,
            facts_extension=self._facts,
            reasoning_cache=ReasoningCache() if self._reasoning_rt else None,
            pattern=None if self._pattern == REACT else self._pattern,
            pattern_memory=memory,
        )
        fingerprint = config_fingerprint(
            answer_extension=True,
            facts_extension=self._facts,
            reasoning_roundtrip=self._reasoning_rt,
            agent_pattern=self._pattern,
        )
        decisions = 0  # 에피소드 전체의 승인 결정 수(정책 순번)
        last_run: uuid.UUID | None = None
        try:
            for index, turn in enumerate(t for t in episode.task.turns if t.role == "user"):
                submission.answer = None  # 성공 판정은 마지막 run의 제출이다
                try:
                    last_run = await _start_run(deps, database, turn, fingerprint)
                    decisions = await _drive(
                        deps, database, last_run, episode.task.approval_policy, decisions
                    )
                finally:  # 턴별 구조화된 답 기록(ADR-0029). 실패한 턴에도 남긴다
                    episode.trace.record_turn_structured(index, submission.answer)
            text = await _assistant_text(deps, database, last_run) if last_run is not None else ""
        except Exception:
            self._raise_budget_error()  # 도구가 삼킨 질의 임베딩 상한 초과를 우선한다
            raise
        self._raise_budget_error()
        return EpisodeOutput(
            final_text=text,
            structured=submission.answer,
            pattern_stats=memory.stats.to_dict() if memory is not None else None,
        )

    def _raise_budget_error(self) -> None:
        """질의 임베딩이 비용 상한을 넘었으면 에피소드 실패가 아니라 실험 중단으로 알린다."""
        if self._index is not None and self._index.budget_error is not None:
            raise self._index.budget_error

    def _watch(self, llm: LLM, episode: Episode) -> LLM:
        if self._index is None:
            return llm
        return _IndexWatchLLM(llm, self._index, episode.task.account_id)

    def _mail_index(
        self, episode: Episode, database: EpisodeDatabase, clock: FixedClock
    ) -> MailIndexDeps | None:
        """`--index on`이면 과제 계정의 색인에 고정된 의존성, off면 None(`index_unavailable`)."""
        if self._index is None:
            return None
        return self._index.mail_index_for(
            account_id=episode.task.account_id,
            db=database.db,
            clock=clock,
            ids=database.ids,
            episode_workspace=database.workspace_id,
        )


def _require_drive(episode: Episode) -> Any:
    drive = episode.connectors.drive
    if drive is None:
        raise RuntimeError("runflow_v0에는 문서 커넥터가 필요하다")
    return drive


async def _start_run(
    deps: RunDeps, database: EpisodeDatabase, turn: SutTurn, fingerprint: str
) -> uuid.UUID:
    """run 생성과 사용자 메시지 기록. `POST /runs`가 하는 기록과 같고 작업 큐 등록만 없다."""
    ws = database.workspace_id
    async with deps.db.workspace_session(ws) as s:
        run = await create_run(
            s,
            workspace_id=ws,
            conversation_id=database.conversation_id,
            pattern=PATTERN,
            config_fingerprint=fingerprint,
            clock=deps.clock,
            ids=deps.ids,
        )
        await append_message(
            s,
            workspace_id=ws,
            run_id=run.id,
            role="user",
            content={"text": turn.content},
            clock=deps.clock,
        )
    return run.id


async def _drive(
    deps: RunDeps,
    database: EpisodeDatabase,
    run_id: uuid.UUID,
    policy: tuple[str, ...],
    decisions: int,
) -> int:
    """run이 끝날 때까지 구동한다. 승인 대기마다 정책대로 결정한다. 누적 결정 수를 돌려준다."""
    ws = database.workspace_id
    approvals_in_run = 0
    for _ in range(MAX_DRIVE_ROUNDS):
        await advance_run(deps, ws, run_id)
        async with deps.db.workspace_session(ws) as s:
            run = await get_run(s, workspace_id=ws, run_id=run_id)
            approval = await latest_approval_for_run(s, ws, run_id)
        if run is None:
            raise RunFailed("run이 없다")
        if run.status in TERMINAL_RUN_STATUSES:
            if run.status != "succeeded":
                raise RunFailed(run.status)
            return decisions
        if (
            run.status == "waiting_approval"
            and approval is not None
            and approval.status == "pending"
        ):
            if approvals_in_run >= MAX_APPROVALS_PER_RUN:
                raise ApprovalLimitExceeded(str(MAX_APPROVALS_PER_RUN))
            await _decide(deps, ws, run_id, approval.id, approve=decision_for(policy, decisions))
            approvals_in_run += 1
            decisions += 1
    raise RunNotSettled(str(MAX_DRIVE_ROUNDS))


async def _decide(
    deps: RunDeps, ws: uuid.UUID, run_id: uuid.UUID, approval_id: uuid.UUID, *, approve: bool
) -> None:
    """시뮬레이션 사용자의 결정을 기록한다. 실사용 API와 같은 기록 함수, 잠금은 run -> approval."""
    async with deps.db.workspace_session(ws) as s:
        if await lock_run(s, ws, run_id) != "waiting_approval":
            raise RuntimeError("ApprovalNotWaiting")
        decided = await decide_approval(
            s, workspace_id=ws, approval_id=approval_id, approve=approve, clock=deps.clock
        )
        if not decided:
            raise RuntimeError("ApprovalNotDecided")


async def _assistant_text(deps: RunDeps, database: EpisodeDatabase, run_id: uuid.UUID) -> str:
    """마지막 run의 비서 텍스트를 step 순으로 `\\n\\n`으로 잇는다(화면에 보이는 응답과 같다)."""
    ws = database.workspace_id
    async with deps.db.workspace_session(ws) as s:
        rows = await list_messages(s, workspace_id=ws, run_id=run_id)
    parts = [
        str(r.content["text"])
        for r in rows
        if r.role == "assistant"
        and isinstance(r.content.get("text"), str)
        and r.content["text"].strip()
    ]
    return "\n\n".join(parts)
