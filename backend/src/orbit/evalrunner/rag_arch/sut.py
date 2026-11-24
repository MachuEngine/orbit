"""평가 대상 `rag_arch`: RAG 아키텍처 비교(ADR-0036)의 A0·A1·A2·A4(GraphRAG).

- A1: 재작성 LLM -> Hybrid 20통 -> reranker 5통 -> 압축 LLM -> 답 LLM(3회).
- A0: 질문 원문으로 Hybrid 검색 1회, 대표 청크 기준 상위 k개 메일의 원문을 맥락 상한까지 넣고
  답변 LLM을 1회 부른다. 모델은 메일 핸들(M1 ...)만 본다. 모델 입력에 넣은 문맥(머리글+본문,
  상한 적용 뒤)은 A2처럼 trace의 `read_mail` 결과로 남겨 Judge가 같은 방식으로 읽는다(합성 데이터).
- A2: 기존 `runflow_v0` ReAct를 `search_indexed_mail`·`read_mail`·`submit_answer`로 제한한다.
  제한은 `RunDeps.tool_limits`가 도구 목록과 실행 코드에서 함께 강제한다.

- A4: 경로 선택(LLM 1회, 강제 가능) -> local(개체·관계 이웃 메일 원문) 또는 global(커뮤니티 요약)
  -> A0와 같은 답 흐름. 그래프는 `graph build`로 미리 만든 색인에서만 읽는다.

- ORACLE: 진단 전용(주 비교 제외). 검색 없이 dev gold 메일 원문만 A0 답 흐름에 넣는다.
- A3: 라우터 LLM 1회(질문 문장만) -> {none(검색 없이 답 1회), A1, A2} 중 하나를 그대로 실행.

에피소드 진단은 `EpisodeOutput.rag_diag`로 낸다.
`rag_diag`에는 ID와 수치만 둔다(ADR-0006).
"""

import asyncio
import json
import time
from dataclasses import replace
from typing import Any, cast
from zoneinfo import ZoneInfo

from orbit.assistant.agent import TEMPERATURE
from orbit.assistant.envelope import boundary_for, wrap_data
from orbit.assistant.tools import READ_MAIL, SUBMIT_ANSWER, ReadOnlyTools
from orbit.assistant.workset import WorkSet
from orbit.core.clock import FixedClock
from orbit.evalrunner.episode import Episode, EpisodeOutput
from orbit.evalrunner.rag_arch import a3
from orbit.evalrunner.rag_arch import local_rank as lr
from orbit.evalrunner.rag_arch.a1 import (
    rerank_order,
    rerank_passage,
)
from orbit.evalrunner.rag_arch.a1_steps import compress_mails, rewrite_query
from orbit.evalrunner.rag_arch.agentic_search import AgenticSearch
from orbit.evalrunner.rag_arch.answer_norm import RAG_ANSWER_NORM_VERSION
from orbit.evalrunner.rag_arch.config import (
    A0_PROMPT_VERSION,
    A1_COMPRESS_PROMPT,
    A1_COMPRESS_VERSION,
    A1_PROMPT_VERSION,
    A1_REWRITE_PROMPT,
    A1_REWRITE_VERSION,
    A2_TOOLS,
    A2R_SEARCH_VERSION,
    A2X_SEARCH_VERSION,
    A2X_TOOL_NOTE,
    A2X_TOOL_NOTE_VERSION,
    A3_FALLBACK_ROUTE,
    A3_ROUTE_MAX_OUTPUT_TOKENS,
    A3_ROUTE_PROMPT,
    A3_ROUTE_VERSION,
    ANSWER_FLOW_VERSION,
    ARCHS,
    CANDIDATES,
    COMPRESS_MAX_OUTPUT_TOKENS,
    DEFAULT_CONTEXT_CHAR_CAP,
    GRAPH_RERANK_CANDIDATES,
    LOCAL_RANK_VERSION,
    ORACLE_CONTEXT_VERSION,
    RAG_ANSWER_NOTE,
    RAG_ANSWER_NOTE_VERSION,
    RAG_ARCH_VERSION,
    RERANK_TOP_K,
    RERANKER_DEVICE,
    RERANKER_MAX_LENGTH,
    RETRIEVAL_K,
    REWRITE_MAX_OUTPUT_TOKENS,
    TOP_K,
    a0_prompt_sha,
    build_a0_system,
    condition_digest,
    prompt_sha,
)
from orbit.evalrunner.rag_arch.graph import prompts as gp
from orbit.evalrunner.rag_arch.graph import query as gq
from orbit.evalrunner.rag_arch.graph import store as gs
from orbit.evalrunner.rag_arch.reranker import LocalCrossEncoderReranker, Reranker
from orbit.evalrunner.runflow_index import EvalIndexProvider
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.llm.types import LLMRequest, Message
from orbit.mailindex.limits import DEFAULT_RESULTS, MAX_QUERY_CHARS
from orbit.runflow.answer import ANSWER_EXTENSION_VERSION, answer_tool_spec
from orbit.runflow.index_tool import IndexedMailSearch
from orbit.runflow.prompt import PROMPT_VERSION
from orbit.runflow.tool_limits import READ_NAME, ToolLimits


class ArchSearchFailed(RuntimeError):  # noqa: N818  도메인 용어
    """A0의 검색이 오류이거나 색인이 비었거나 준비 중이다. 빈·부분 맥락으로 답하게 두지 않는다."""


def empty_compressed(
    top: list[str], mails: dict[str, dict[str, Any]], compressed: dict[str, str]
) -> list[str]:
    """원래 본문이 있었는데 압축 뒤 비어 버린 메일 핸들. 공백뿐인 원래 본문은 빈 본문으로 본다."""
    return [h for h in top if str(mails[h].get("body") or "").strip() and not compressed[h]]


def oracle_message_ids(dataset: Any, split: str = "dev") -> dict[str, tuple[str, ...]]:
    """dev 과제 ID -> gold 정답 메일 ID(gold 순서, 중복 제거). message_set이 아니면 빈 튜플이다.

    `SutTask`는 gold를 숨기므로 CLI가 dev 과제에서만 만들어 ORACLE SUT에 넘긴다."""
    out: dict[str, tuple[str, ...]] = {}
    for task in dataset.tasks:
        if task.split != split:
            continue
        answer = task.gold.expected_answer
        messages = (
            getattr(answer, "messages", None)
            if getattr(answer, "kind", "") == "message_set"
            else None
        )
        out[task.task_id] = tuple(dict.fromkeys(m.message_id for m in messages or []))
    return out


class RagArchSUT:
    name = "rag_arch"

    def __init__(
        self,
        *,
        rag_arch: str,
        index: EvalIndexProvider | None = None,
        index_mode: str = "on",
        context_char_cap: int = DEFAULT_CONTEXT_CHAR_CAP,
        reranker: Reranker | None = None,
        graph_digests: dict[str, str] | None = None,
        a4_mode: str = "auto",
        oracle_gold: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        if rag_arch not in ARCHS:
            raise ValueError(f"지원하지 않는 RAG 아키텍처: {rag_arch!r} (지원: {ARCHS})")
        if index_mode != "on" or index is None:
            raise ValueError("rag_arch는 --index on(색인 제공자)에서만 쓴다")
        if isinstance(context_char_cap, bool) or context_char_cap < 1:
            raise ValueError("맥락 글자 상한은 1 이상이어야 한다")
        if a4_mode not in ("auto", *gq.MODES):
            raise ValueError(f"--a4-mode는 auto|local|global이어야 한다: {a4_mode!r}")
        if rag_arch in ("A4", "A4R") and not graph_digests:
            raise ValueError(f"{rag_arch}는 구축된 그래프의 계정별 지문(graph_digests)이 필요하다")
        if rag_arch == "ORACLE" and oracle_gold is None:
            raise ValueError("ORACLE는 dev 과제의 정답 메일 ID(oracle_gold)가 필요하다")
        self._oracle_gold = oracle_gold
        self._graph_digests = dict(graph_digests or {})
        self._a4_mode = a4_mode
        self._arch = rag_arch
        self._index = index
        self._cap = context_char_cap
        # A1만 쓴다. 에피소드마다 SUT를 새로 만들므로 모델 재로딩을 피하려면 호출자가 하나를 넘긴다
        self._reranker: Reranker = reranker or LocalCrossEncoderReranker()
        self._inner = (
            RunflowV0SUT(
                index_mode="on",
                index=index,
                limits_factory=lambda: ToolLimits(A2_TOOLS, self._cap),
                answer_note=RAG_ANSWER_NOTE,
            )
            if rag_arch in ("A2", "A3", "A2R", "A2X")
            else None
        )

    @property
    def version(self) -> str:
        prompt = {
            "A0": A0_PROMPT_VERSION,
            "ORACLE": f"{ORACLE_CONTEXT_VERSION}+{A0_PROMPT_VERSION}",
            "A1": f"{A1_PROMPT_VERSION}+{A1_REWRITE_VERSION}+{A1_COMPRESS_VERSION}",
            "A3": (
                f"{A3_ROUTE_VERSION}+none-{A0_PROMPT_VERSION}"
                f"+{A1_PROMPT_VERSION}+{A1_REWRITE_VERSION}+{A1_COMPRESS_VERSION}"
                f"+{PROMPT_VERSION}+{ANSWER_EXTENSION_VERSION}"
            ),
            "A4": f"{gp.A4_PROMPT_VERSION}+{gp.ROUTE_VERSION}+mode-{self._a4_mode}",
            "A4R": (
                f"{gp.A4_PROMPT_VERSION}+{gp.ROUTE_VERSION}+mode-{self._a4_mode}"
                f"+{LOCAL_RANK_VERSION}"
            ),
            "A4RN": f"{gp.A4_PROMPT_VERSION}+{LOCAL_RANK_VERSION}+graph-none",
            "A2R": f"{PROMPT_VERSION}+{ANSWER_EXTENSION_VERSION}+{A2R_SEARCH_VERSION}",
            "A2X": (
                f"{PROMPT_VERSION}+{ANSWER_EXTENSION_VERSION}+{A2X_SEARCH_VERSION}"
                f"+{A2X_TOOL_NOTE_VERSION}+{A1_REWRITE_VERSION}+{A1_COMPRESS_VERSION}"
            ),
        }.get(self._arch, f"{PROMPT_VERSION}+{ANSWER_EXTENSION_VERSION}")
        head = f"{RAG_ARCH_VERSION}+{self._arch}+{prompt}+{RAG_ANSWER_NOTE_VERSION}"
        return f"{head}+cap{self._cap}+index-on"

    def config_info(
        self, *, model_id: str, llm_info: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """설정 지문에 더할 항목. 아키텍처·프롬프트 버전·k·맥락 상한·모델 ID를 모두 담는다."""
        a0 = self._arch == "A0"
        oracle = self._arch == "ORACLE"  # 답 프롬프트·흐름은 A0와 같고 맥락만 gold다
        a3 = self._arch == "A3"
        a1 = self._arch in ("A1", "A3")  # A3는 A1 경로의 상수를 모두 지문에 넣는다
        a4 = self._arch in ("A4", "A4R")  # A4R은 A4의 그래프 항목을 모두 지문에 넣는다
        rn = self._arch == "A4RN"  # 그래프 없음. 답 프롬프트·흐름은 A4와 같다
        ranked = self._arch in ("A4R", "A4RN")
        agentic = self._arch in ("A2R", "A2X")  # A2의 모든 항목 + 검색 도구 실행 항목
        advanced = self._arch == "A2X"
        core: dict[str, Any] = {
            "rag_arch": self._arch,
            "rag_arch_version": RAG_ARCH_VERSION,
            "answer_norm_version": RAG_ANSWER_NORM_VERSION,
            "rag_answer_note_sha": prompt_sha(RAG_ANSWER_NOTE),
            "prompt_version": A0_PROMPT_VERSION
            if a0 or oracle
            else A3_ROUTE_VERSION
            if a3
            else A1_PROMPT_VERSION
            if a1
            else gp.A4_PROMPT_VERSION
            if a4 or rn
            else PROMPT_VERSION,
            "top_k": TOP_K
            if a0
            else None
            if oracle  # 검색 없음. gold 메일 전부를 상한까지 넣는다
            else RERANK_TOP_K
            if a1
            or advanced  # A3의 k는 A1 경로 값이고 A2 경로의 k는 a2_default_results에 따로 둔다
            else gp.LOCAL_MAX_MAILS
            if a4 or rn
            else DEFAULT_RESULTS,  # A2의 k는 검색 도구의 기본값(모델이 바꿀 수 있다)
            "context_char_cap": self._cap,
            "model_id": model_id,
        }
        if a0 or a1 or a4 or rn or oracle:  # a1은 A3 포함
            core["answer_flow_version"] = ANSWER_FLOW_VERSION
        if oracle:
            core["oracle_context_version"] = ORACLE_CONTEXT_VERSION
        if a0:
            core["retrieval_k"] = RETRIEVAL_K
        if a1:
            core.update(
                rewrite_version=A1_REWRITE_VERSION,
                rewrite_prompt_sha=prompt_sha(A1_REWRITE_PROMPT),
                rewrite_max_output_tokens=REWRITE_MAX_OUTPUT_TOKENS,
                compress_version=A1_COMPRESS_VERSION,
                compress_prompt_sha=prompt_sha(A1_COMPRESS_PROMPT),
                compress_max_output_tokens=COMPRESS_MAX_OUTPUT_TOKENS,
                candidates=CANDIDATES,
                rerank_top_k=RERANK_TOP_K,
                reranker={
                    "model_id": self._reranker.model_id,
                    "revision": self._reranker.revision,
                    "max_length": getattr(self._reranker, "max_length", RERANKER_MAX_LENGTH),
                    "device": getattr(self._reranker, "device", RERANKER_DEVICE),
                },
            )
        if a3:
            core.update(
                route_version=A3_ROUTE_VERSION,
                route_prompt_sha=prompt_sha(A3_ROUTE_PROMPT),
                route_max_output_tokens=A3_ROUTE_MAX_OUTPUT_TOKENS,
                route_fallback=A3_FALLBACK_ROUTE,
                a1_prompt_version=A1_PROMPT_VERSION,
                none_prompt_version=A0_PROMPT_VERSION,
                a2_prompt_version=(
                    f"{PROMPT_VERSION}+{ANSWER_EXTENSION_VERSION}+{RAG_ANSWER_NOTE_VERSION}"
                ),
                a2_default_results=DEFAULT_RESULTS,
                a2_allowed_tools=sorted(A2_TOOLS),
            )
        if agentic:
            core.update(
                search_variant="advanced" if advanced else "rerank",
                search_version=A2X_SEARCH_VERSION if advanced else A2R_SEARCH_VERSION,
                candidates=CANDIDATES,
                reranker={
                    "model_id": self._reranker.model_id,
                    "revision": self._reranker.revision,
                    "max_length": getattr(self._reranker, "max_length", RERANKER_MAX_LENGTH),
                    "device": getattr(self._reranker, "device", RERANKER_DEVICE),
                },
            )
        if advanced:
            core.update(
                rerank_top_k=RERANK_TOP_K,
                tool_note_version=A2X_TOOL_NOTE_VERSION,
                tool_note_sha=prompt_sha(A2X_TOOL_NOTE),
                rewrite_version=A1_REWRITE_VERSION,
                rewrite_prompt_sha=prompt_sha(A1_REWRITE_PROMPT),
                rewrite_max_output_tokens=REWRITE_MAX_OUTPUT_TOKENS,
                compress_version=A1_COMPRESS_VERSION,
                compress_prompt_sha=prompt_sha(A1_COMPRESS_PROMPT),
                compress_max_output_tokens=COMPRESS_MAX_OUTPUT_TOKENS,
            )
        if a4:
            core.update(
                extract_version=gp.EXTRACT_VERSION,
                extract_prompt_sha=prompt_sha(gp.EXTRACT_PROMPT),
                summary_version=gp.SUMMARY_VERSION,
                summary_prompt_sha=prompt_sha(gp.SUMMARY_PROMPT),
                route_prompt_sha=prompt_sha(gp.ROUTE_PROMPT),
                louvain_seed=gp.LOUVAIN_SEED,
                hops=gp.HOPS,
                a4_mode=self._a4_mode,
                local_max_mails=gp.LOCAL_MAX_MAILS,
                global_max_communities=gp.GLOBAL_MAX_COMMUNITIES,
                global_sources_per_community=gp.GLOBAL_SOURCES_PER_COMMUNITY,
                name_match_limit=gp.NAME_MATCH_LIMIT,
                embed_match_limit=gp.EMBED_MATCH_LIMIT,
                min_community_entities=gp.MIN_COMMUNITY_ENTITIES,
                max_summary_relations=gp.MAX_SUMMARY_RELATIONS,
                max_entities_per_mail=gp.MAX_ENTITIES_PER_MAIL,
                max_relations_per_mail=gp.MAX_RELATIONS_PER_MAIL,
                max_name_chars=gp.MAX_NAME_CHARS,
                max_predicate_chars=gp.MAX_PREDICATE_CHARS,
                extract_max_output_tokens=gp.EXTRACT_MAX_OUTPUT_TOKENS,
                summary_max_output_tokens=gp.SUMMARY_MAX_OUTPUT_TOKENS,
                graph_digests=dict(sorted(self._graph_digests.items())),
            )
        if ranked:
            core.update(
                local_rank_version=LOCAL_RANK_VERSION,
                graph_rerank_candidates=GRAPH_RERANK_CANDIDATES,
                local_max_mails=gp.LOCAL_MAX_MAILS,
                reranker={
                    "model_id": self._reranker.model_id,
                    "revision": self._reranker.revision,
                    "max_length": getattr(self._reranker, "max_length", RERANKER_MAX_LENGTH),
                    "device": getattr(self._reranker, "device", RERANKER_DEVICE),
                },
            )
        if rn:
            core["graph"] = "none"
        info = {
            **core,
            "condition_digest": condition_digest(core),
            "index": self._index.config_info(),
        }
        if a0 or a1 or a4 or rn or oracle:
            info["prompt_sha"] = a0_prompt_sha()  # A1 답 프롬프트 본문은 A0와 같다(버전은 따로)
        if a3:
            info["inner"] = cast(RunflowV0SUT, self._inner).config_info(
                model_id=model_id, llm_info=llm_info
            )
        elif not (a0 or a1 or a4 or rn or oracle):
            info["allowed_tools"] = sorted(A2_TOOLS)
            info["inner"] = cast(RunflowV0SUT, self._inner).config_info(
                model_id=model_id, llm_info=llm_info
            )
        return info

    async def run(self, episode: Episode) -> EpisodeOutput:
        if self._arch == "A3":
            return await self._run_a3(episode)
        if self._arch in ("A2R", "A2X"):
            return await self._run_agentic(episode)
        if self._inner is not None:
            return await self._inner.run(episode)
        return await self._run_direct(episode)

    async def _run_a3(self, episode: Episode) -> EpisodeOutput:
        """A3: 라우터 1회(질문 문장만) 뒤 고른 경로를 기존 흐름 그대로 실행한다."""
        question = self._single_question(episode)
        episode.steps.consume()
        route, route_diag = await a3.route_question(episode.llm, question, episode.task.task_id)
        if route == "A2":
            out = await cast(RunflowV0SUT, self._inner).run(episode)
            diag = out.rag_diag or {}
        else:
            out = await self._run_direct(episode, "A1" if route == "A1" else "none")
            diag = out.rag_diag or {}
        # 고른 경로가 낸 진단은 그대로 두고 arch만 A3로 맞춘다
        return replace(out, rag_diag={**diag, "arch": "A3", **route_diag})

    async def _run_agentic(self, episode: Episode) -> EpisodeOutput:
        """A2R·A2X: A2의 ReAct 흐름 그대로, 검색 도구 실행만 `AgenticSearch`로 바꾼다(ADR-0038)."""
        database = episode.database
        if database is None:
            raise RuntimeError("rag_arch에는 러너가 준비한 평가 DB 핸들이 필요하다")
        advanced = self._arch == "A2X"
        search = AgenticSearch(
            variant="advanced" if advanced else "rerank",
            episode=episode,
            mi=self._index.mail_index_for(
                account_id=episode.task.account_id,
                db=database.db,
                clock=FixedClock(episode.task.as_of_time),
                ids=database.ids,
                episode_workspace=database.workspace_id,
            ),
            tz=ZoneInfo(episode.task.time_zone),
            reranker=self._reranker,
            rewrite=self._rewrite,
            compress=self._compress,
            raise_budget_error=self._raise_budget_error,
        )
        inner = RunflowV0SUT(
            index_mode="on",
            index=self._index,
            limits_factory=lambda: ToolLimits(
                A2_TOOLS,
                self._cap,
                search_override=search,
                search_note=A2X_TOOL_NOTE if advanced else None,
            ),
            answer_note=RAG_ANSWER_NOTE,
        )
        out = await inner.run(episode)
        return replace(out, rag_diag={**(out.rag_diag or {}), "arch": self._arch, **search.diag()})

    def _single_question(self, episode: Episode) -> str:
        questions = [t.content for t in episode.task.turns if t.role == "user"]
        if not questions:
            raise RuntimeError("사용자 질문이 없다")
        if len(questions) > 1:  # 단일 질문만 받는다. 마지막 턴만 쓰고 넘어가지 않는다
            raise RuntimeError(f"{self._arch}는 질문이 2턴 이상인 과제를 지원하지 않는다")
        return questions[0]

    async def _run_direct(self, episode: Episode, path: str | None = None) -> EpisodeOutput:
        """A0·A1(A3의 A1·none 경로 포함). 답 생성 전까지의 흐름만 다르고, 답 호출과 기록은 같다.

        `path`는 A3가 고른 경로(`A1`|`none`)다. none은 검색 없이 빈 맥락으로 답한다."""
        mode = path or self._arch
        database = episode.database
        if database is None:
            raise RuntimeError("rag_arch에는 러너가 준비한 평가 DB 핸들이 필요하다")
        clock = FixedClock(episode.task.as_of_time)
        tz = ZoneInfo(episode.task.time_zone)
        mail = episode.connectors.mail
        question = self._single_question(episode)
        workset = WorkSet()
        # ToolLimits는 상한·진단용이다. allowed가 비어 있어도 도구 제한에는 쓰지 않는다
        limits = ToolLimits(frozenset(), self._cap)
        reader = ReadOnlyTools(
            calendar=episode.connectors.calendar, mail=mail, tz=tz, workset=workset
        )
        boundary = boundary_for(f"{self._arch.lower()}:{episode.task.task_id}")
        extra: dict[str, Any] = {}
        try:
            mi = self._index.mail_index_for(
                account_id=episode.task.account_id,
                db=database.db,
                clock=clock,
                ids=database.ids,
                episode_workspace=database.workspace_id,
            )
            if mode == "A0":
                # recall@K 진단용으로 10개를 찾고 답 입력에는 상위 TOP_K개만 넣는다
                hits = await self._search(episode, mi, tz, workset, question, RETRIEVAL_K, TOP_K)
                limits.search_calls = 1
                blocks = await self._context(
                    episode, reader, limits, workset, boundary, [h["handle"] for h in hits[:TOP_K]]
                )
                extra = {"ranked_message_ids": [workset.resolve_message(h["handle"]) for h in hits]}
            elif mode == "none":
                blocks, extra = [], {}
            elif mode == "ORACLE":
                # gold를 읽는 유일한 구성이다. dev 과제만 들어 있는 표에 없으면(test 등) 거부한다
                ids = cast(dict[str, tuple[str, ...]], self._oracle_gold).get(episode.task.task_id)
                if ids is None:
                    raise ValueError("ORACLE은 dev 분할의 과제에서만 쓴다")
                blocks = await self._context(
                    episode, reader, limits, workset, boundary,
                    [workset.message_handle(m) for m in ids],
                )  # fmt: skip
                extra = {}
            elif mode in ("A4", "A4R"):
                blocks, extra = await self._a4_context(
                    episode, mi, reader, limits, workset, boundary, question
                )
            elif mode == "A4RN":
                blocks, extra = await self._a4rn_context(
                    episode, mi, reader, limits, workset, boundary, question
                )
            else:
                blocks, extra = await self._a1_context(
                    episode, mi, tz, workset, reader, limits, boundary, question
                )
            episode.steps.consume()
            base = (
                Message(role="system", content=build_a0_system(clock.now(), tz, boundary)),
                Message(role="user", content="\n\n".join([question, *blocks])),
            )
            response = await episode.llm.complete(
                LLMRequest(
                    messages=base,
                    purpose="system_under_test",
                    tools=(answer_tool_spec(),),
                    tool_choice="auto",
                    temperature=TEMPERATURE,
                    metadata={"task_id": episode.task.task_id},
                )
            )
        except Exception:
            self._raise_budget_error()
            raise
        self._raise_budget_error()
        structured: dict[str, Any] | None = None
        results: list[Message] = []
        submitted_any = False
        for call in response.tool_calls:
            if call.name != SUBMIT_ANSWER:  # 실행하지 않는다. 호출 짝만 맞춘다
                content = json.dumps({"error": "tool_not_allowed"})
            else:
                submitted_any = True
                submitted = await reader.execute(SUBMIT_ANSWER, dict(call.arguments))
                structured = submitted.structured  # 마지막 제출이 유효하다. 형식 오류면 None
                content = submitted.content
                episode.trace.record_tool(
                    SUBMIT_ANSWER, call.arguments, content, is_error=submitted.is_error
                )
            results.append(Message(role="tool", tool_call_id=call.id, content=content))
        text = response.text or ""
        # 모델이 도구만 내고 답 문장을 비웠을 때만 도구 결과 뒤 한 턴을 더 준다(A2의 다음 턴과 같다)
        followup = submitted_any and not text.strip()
        if followup:
            try:
                episode.steps.consume()
                second = await episode.llm.complete(
                    LLMRequest(
                        messages=(
                            *base,
                            Message(
                                role="assistant",
                                content=response.text,
                                tool_calls=response.tool_calls,
                                provider_items=response.provider_items,
                            ),
                            *results,
                        ),
                        purpose="system_under_test",
                        tools=(answer_tool_spec(),),
                        tool_choice="none",
                        temperature=TEMPERATURE,
                        metadata={"task_id": episode.task.task_id, "step_kind": "answer_followup"},
                    )
                )
            except Exception:
                self._raise_budget_error()
                raise
            self._raise_budget_error()
            text = second.text or ""  # 이 응답의 tool_calls는 무시한다
        episode.trace.record_handles(workset.export())
        episode.trace.record_turn_structured(0, structured)
        return EpisodeOutput(
            final_text=text,
            structured=structured,
            rag_diag={**limits.diag(self._arch), **extra, "answer_followup": followup},
        )

    async def _search(
        self,
        episode: Episode,
        mi: Any,
        tz: ZoneInfo,
        workset: WorkSet,
        query: str,
        max_results: int,
        trace_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid 검색 1회. 오류·빈 색인·준비 중이면 `ArchSearchFailed`. trace에 excerpt는 뺀다.

        `trace_k`가 있으면 trace에는 상위 `trace_k`개만 남긴다(모델 입력에 들어간 결과와 맞춘다)."""
        args = {"query": query[:MAX_QUERY_CHARS], "max_results": max_results}
        found = await IndexedMailSearch(mi, tz).execute(
            args,
            workspace_id=cast(Any, episode.database).workspace_id,
            workset=workset,
            mail=episode.connectors.mail,
        )
        self._raise_budget_error()  # 도구가 삼킨 질의 임베딩 상한 초과를 우선한다
        hits = json.loads(found.content)
        if found.is_error or hits.get("index_empty") or hits.get("index_pending"):
            raise ArchSearchFailed("search")
        # 검색 기록은 A2와 같은 형식이다. 본문 조각(excerpt)은 모델 입력에 넣지 않았으므로 뺀다
        # trace에는 모델에 넘긴 결과 수(trace_k=5)를 기록한다. 10개 검색은 recall@K 진단용이라
        # rag_diag ranked_message_ids에만 남긴다(A2 등과 trace 비교 기준 유지).
        shown = {**hits, "results": [{**h, "excerpt": None} for h in hits["results"][:trace_k]]}
        episode.trace.record_tool(
            "search_indexed_mail",
            {**args, "max_results": trace_k or max_results},
            json.dumps(shown, ensure_ascii=False, sort_keys=True),
            is_error=False,
        )
        return list(hits["results"])

    async def _context(
        self,
        episode: Episode,
        reader: ReadOnlyTools,
        limits: ToolLimits,
        workset: WorkSet,
        boundary: str,
        handles: list[str],
        override: dict[str, str] | None = None,
    ) -> list[str]:
        """순서대로 `read_mail` 결과를 상한에 통과시켜 trace에 남기고 모델 입력 블록을 만든다.

        `override`가 있으면 그 핸들의 본문을 바꿔 넣는다(A1 압축). 상한은 뒤쪽 메일부터 잘린다.
        """
        blocks: list[str] = []
        for handle in handles:
            read = await reader.execute(READ_MAIL, {"handle": handle})
            if read.is_error:
                raise ArchSearchFailed("read")
            raw = read.content
            if override is not None:
                data = json.loads(raw)
                data["body"] = override[handle]
                raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
            content = limits.filter_result(READ_NAME, raw, workset)
            if workset.resolve_message(handle) in limits.reached:
                episode.trace.record_tool(READ_MAIL, {"handle": handle}, content, is_error=False)
                blocks.append(wrap_data(content, source=READ_MAIL, boundary=boundary))
        return blocks

    async def _a1_context(
        self,
        episode: Episode,
        mi: Any,
        tz: ZoneInfo,
        workset: WorkSet,
        reader: ReadOnlyTools,
        limits: ToolLimits,
        boundary: str,
        question: str,
    ) -> tuple[list[str], dict[str, Any]]:
        """A1: 재작성(LLM) -> 후보 20통 -> reranker 상위 5통 -> 압축(LLM) -> 상한. 진단도 낸다."""
        query, rewrite_fallback = await self._rewrite(episode, question)
        hits = await self._search(episode, mi, tz, workset, query, CANDIDATES)
        limits.search_calls = 1
        handles = [h["handle"] for h in hits]
        mails: dict[str, dict[str, Any]] = {}
        for handle in handles:  # 재정렬 입력용. trace에는 모델에 넣은 5통만 남긴다
            read = await reader.execute(READ_MAIL, {"handle": handle})
            if read.is_error:
                raise ArchSearchFailed("read")
            mails[handle] = json.loads(read.content)
        top: list[str] = []
        ranked: list[str] = []
        compressed: dict[str, str] = {}
        fallbacks = 0
        if handles:  # 후보가 0통이면 재정렬·압축을 건너뛰고 빈 맥락으로 답한다
            passages = [rerank_passage(mails[h]) for h in handles]
            scores = await asyncio.to_thread(self._reranker.score, question, passages)
            if len(scores) != len(passages):
                raise RuntimeError("reranker 점수 개수가 후보 수와 다르다")
            order = rerank_order(scores, len(scores))
            ranked = [handles[i] for i in order]
            top = ranked[:RERANK_TOP_K]
            compressed, fallbacks = await self._compress(episode, question, top, mails, boundary)
        blocks = await self._context(episode, reader, limits, workset, boundary, top, compressed)
        before = sum(len(str(mails[h].get("body", ""))) for h in top)
        empties = empty_compressed(top, mails, compressed)
        diag = {
            "rewrite_fallback": rewrite_fallback,
            "first_stage_ranked_message_ids": [workset.resolve_message(h) for h in handles],
            "ranked_message_ids": [workset.resolve_message(h) for h in ranked],
            "reranked_message_ids": [workset.resolve_message(h) for h in top],
            "compress_fallback": fallbacks,
            # 압축 뒤 본문이 빈 메일(머리글만 들어간다). 원래 빈 본문은 뺀다. M 핸들만 남긴다
            "compressed_empty": len(empties),
            "compressed_empty_handles": empties,
            "compressed_chars_before": before,
            "compressed_chars_after": sum(len(b) for b in compressed.values()),
        }
        return blocks, diag

    async def _rewrite(self, episode: Episode, question: str) -> tuple[str, bool]:
        return await rewrite_query(episode, question)

    async def _a4_context(
        self,
        episode: Episode,
        mi: Any,
        reader: ReadOnlyTools,
        limits: ToolLimits,
        workset: WorkSet,
        boundary: str,
        question: str,
    ) -> tuple[list[str], dict[str, Any]]:
        """A4: 경로 선택 -> local(이웃 관계의 메일 원문) 또는 global(커뮤니티 요약) -> 상한."""
        ws = cast(Any, episode.database).workspace_id
        forced = self._a4_mode != "auto"
        fallback = False
        if forced:
            mode = self._a4_mode
        else:
            episode.steps.consume()
            mode, fallback = await gq.route_question(episode.llm, question, episode.task.task_id)
        embedding = (await mi.embedder.embed([question[:MAX_QUERY_CHARS]]))[0]
        self._raise_budget_error()
        extra: dict[str, Any] = {
            "path": mode,
            "route_forced": forced,
            "route_fallback": fallback,
            "graph_digest": self._graph_digests.get(episode.task.account_id),
        }
        async with mi.db.workspace_session(ws) as session:
            entities, communities = await gs.graph_counts(session, ws)
            if mode == "local":
                # local은 그래프가 비어도 실패시키지 않는다. 빈 컨텍스트로 답하고 이유만 남긴다.
                plan = await gq.plan_local(session, ws, question, embedding)
                if self._arch == "A4R":  # 1단계: 도달 메일 전체 -> 임베딩 거리 상위 N통
                    head, first_count = await lr.first_stage(
                        session, ws, embedding, plan.all_mail_ids
                    )
            else:
                if communities == 0:
                    raise ArchSearchFailed("graph_empty")
                hits = await gq.plan_global(session, ws, embedding)
        if mode == "local":
            if self._arch == "A4R":
                top, rerank_ms = await self._second_stage(reader, workset, question, head)
                plan = replace(plan, mail_ids=top)
                extra |= {
                    "first_stage_count": first_count,
                    "rerank_input_count": len(head),
                    "rerank_ms": rerank_ms,
                }
            handles = {m: workset.message_handle(m) for m in plan.mail_ids}
            # 관계 블록의 글자 몫을 먼저 확보하고, 남은 글자로 메일 본문을 채운다.
            lines = gq.relation_lines(plan, handles)
            text = limits.take("\n".join(lines)) if lines else ""
            blocks: list[str] = []
            if text:
                episode.trace.record_tool("graph_context", {"path": "local"}, text, is_error=False)
                blocks = [wrap_data(text, source="graph_relations", boundary=boundary)]
            blocks += await self._context(
                episode, reader, limits, workset, boundary, list(handles.values())
            )
            included, pos = 0, 0
            for line in lines:
                if pos + len(line) > len(text):
                    break
                included += 1
                pos += len(line) + 1
            if entities == 0:
                empty_reason = "graph_empty"
            elif communities == 0:
                empty_reason = "no_community"
            elif plan.seed_count == 0:
                empty_reason = "no_seed"
            elif not plan.mail_ids:
                empty_reason = "no_relation"
            elif lines and included == 0:
                empty_reason = "relation_block_cap"
            else:
                empty_reason = None
            extra |= {
                "entity_seed_count": plan.seed_count,
                "entity_name_matches": plan.name_matches,
                "entity_embed_matches": plan.embed_matches,
                "candidate_message_ids": list(plan.mail_ids),
                "relation_lines": included,
                "empty_reason": empty_reason,
            }
            return blocks, extra
        parts: list[str] = []
        for hit in hits:
            listed = [workset.message_handle(m) for m in hit.provider_item_ids]
            listed = listed[: gp.GLOBAL_SOURCES_PER_COMMUNITY]
            full = f"{hit.summary}\n출처: {' '.join(listed)}"
            kept = limits.take(full)
            if kept:
                parts.append(kept)
                tokens = kept.rsplit("출처:", 1)[-1].split() if "출처:" in kept else []
                if kept != full and not kept.endswith(" "):
                    tokens = tokens[:-1]  # 잘린 마지막 토큰은 온전한 핸들인지 알 수 없다
                shown = set(tokens)
                limits.reached.update(
                    m for m in hit.provider_item_ids if workset.message_handle(m) in shown
                )
        blocks = []
        if parts:
            episode.trace.record_tool(
                "graph_context", {"path": "global"}, "\n\n".join(parts), is_error=False
            )
            blocks = [
                wrap_data(p, source="graph_community_summary", boundary=boundary) for p in parts
            ]
        extra |= {"community_count": len(hits), "community_included": len(parts)}
        return blocks, extra

    async def _second_stage(
        self, reader: ReadOnlyTools, workset: WorkSet, question: str, head: list[str]
    ) -> tuple[list[str], float]:
        """2단계: 1단계 후보의 제목+본문을 reranker로 재정렬한다. (상위 메일 ID, reranker ms)."""
        if not head:
            return [], 0.0
        passages: list[str] = []
        for mail_id in head:  # trace에는 남기지 않는다. 모델에 넣는 메일만 `_context`가 남긴다
            read = await reader.execute(READ_MAIL, {"handle": workset.message_handle(mail_id)})
            if read.is_error:
                raise ArchSearchFailed("read")
            passages.append(rerank_passage(json.loads(read.content)))
        started = time.perf_counter()
        scores = await asyncio.to_thread(self._reranker.score, question, passages)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return lr.second_stage(head, scores), elapsed

    async def _a4rn_context(
        self,
        episode: Episode,
        mi: Any,
        reader: ReadOnlyTools,
        limits: ToolLimits,
        workset: WorkSet,
        boundary: str,
        question: str,
    ) -> tuple[list[str], dict[str, Any]]:
        """A4RN: 그래프·경로 선택 없이 계정 전체 메일을 2단계 순위로 올려 상위 메일을 넣는다."""
        ws = cast(Any, episode.database).workspace_id
        embedding = (await mi.embedder.embed([question[:MAX_QUERY_CHARS]]))[0]
        self._raise_budget_error()
        async with mi.db.workspace_session(ws) as session:
            head, first_count = await lr.first_stage(session, ws, embedding, None)
        if first_count == 0:
            raise ArchSearchFailed("index_empty")
        top, rerank_ms = await self._second_stage(reader, workset, question, head)
        handles = [workset.message_handle(m) for m in top]
        blocks = await self._context(episode, reader, limits, workset, boundary, handles)
        return blocks, {
            "candidate_message_ids": top,
            "first_stage_count": first_count,
            "rerank_input_count": len(head),
            "rerank_ms": rerank_ms,
        }

    async def _compress(
        self,
        episode: Episode,
        question: str,
        handles: list[str],
        mails: dict[str, dict[str, Any]],
        boundary: str,
    ) -> tuple[dict[str, str], int]:
        return await compress_mails(episode, question, handles, mails, boundary)

    def _raise_budget_error(self) -> None:
        if self._index.budget_error is not None:
            raise self._index.budget_error
