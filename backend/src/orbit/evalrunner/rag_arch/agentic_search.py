"""A2R·A2X 검색 도구 실행(ADR-0038). `search_indexed_mail`의 실행만 바꾼다.

- A2R: Hybrid 후보 20통 -> reranker -> 상위 `max_results`통. 결과 형식은 A2와 같다.
- A2X: 에이전트 `query` 재작성 -> Hybrid 20통 -> reranker 상위 5통 -> 문맥 압축.
  결과의 `excerpt`에 압축 본문을 넣는다(필드 상한·데이터 봉투는 발췌와 같다).

후보 메일은 A1처럼 Connector로 읽는다(`workspace_id` 필터는 색인 SQL과 Connector가 건다).
진단(`diag`)에는 메일 ID와 숫자만 둔다. 질의문·본문은 담지 않는다(ADR-0006).
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

from orbit.assistant.envelope import boundary_for
from orbit.assistant.tools import READ_MAIL, ReadOnlyTools, ToolOutcome, limit_external
from orbit.assistant.workset import WorkSet
from orbit.connectors.contracts import MailConnector
from orbit.evalrunner.episode import Episode
from orbit.evalrunner.rag_arch.a1 import rerank_order, rerank_passage
from orbit.evalrunner.rag_arch.config import CANDIDATES, RERANK_TOP_K
from orbit.evalrunner.rag_arch.reranker import Reranker
from orbit.mailindex.limits import MAX_QUERY_CHARS
from orbit.runflow.index_tool import IndexedMailSearch

Rewrite = Callable[[Episode, str], Awaitable[tuple[str, bool]]]
Compress = Callable[
    [Episode, str, list[str], dict[str, dict[str, Any]], str],
    Awaitable[tuple[dict[str, str], int]],
]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class AgenticSearch:
    """에피소드 하나의 검색 도구 실행기. 호출 기록(`diag`)을 이 객체가 모은다."""

    def __init__(
        self,
        *,
        variant: str,
        episode: Episode,
        mi: Any,
        tz: ZoneInfo,
        reranker: Reranker,
        rewrite: Rewrite,
        compress: Compress,
        raise_budget_error: Callable[[], None],
    ) -> None:
        if variant not in ("rerank", "advanced"):
            raise ValueError(f"search_variant는 rerank|advanced여야 한다: {variant!r}")
        self._variant = variant
        self._episode = episode
        self._mi = mi
        self._tz = tz
        self._reranker = reranker
        self._rewrite = rewrite
        self._compress = compress
        self._raise_budget_error = raise_budget_error
        self.calls: list[dict[str, Any]] = []
        self.rewrite_fallback_calls = 0
        self.compress_fallback_mails = 0
        self.ignored_max_results = 0
        self.empty_compressed_mails = 0

    async def __call__(
        self, arguments: object, workspace_id: uuid.UUID, workset: WorkSet, mail: MailConnector
    ) -> ToolOutcome:
        base = IndexedMailSearch(self._mi, self._tz)
        parsed = base._parse(arguments)  # noqa: SLF001  인자 검증은 도구와 같은 규칙을 쓴다
        if parsed is None:
            return await base.execute(
                arguments, workspace_id=workspace_id, workset=workset, mail=mail
            )
        agent_query, limit = parsed
        advanced = self._variant == "advanced"
        if advanced:
            if limit != RERANK_TOP_K:
                self.ignored_max_results += 1  # 5로 고정한다. 에이전트가 다른 값을 넘긴 횟수
            limit = RERANK_TOP_K
        rewrite_fallback = False
        query = agent_query
        if advanced:
            query, rewrite_fallback = await self._rewrite(self._episode, agent_query)
            self.rewrite_fallback_calls += int(rewrite_fallback)
        # 후보 20통은 임시 작업 집합에만 등록한다. 에이전트가 볼 메일만 실제 작업 집합에 등록한다
        scratch = WorkSet()
        found = await base.execute(
            {"query": query[:MAX_QUERY_CHARS], "max_results": CANDIDATES},
            workspace_id=workspace_id,
            workset=scratch,
            mail=mail,
        )
        self._raise_budget_error()  # 도구가 삼킨 질의 임베딩 상한 초과를 우선한다
        data = json.loads(found.content)
        if found.is_error or data.get("index_empty") or "results" not in data:
            return found
        hits: list[dict[str, Any]] = data["results"]
        handles = [h["handle"] for h in hits]
        reader = ReadOnlyTools(
            calendar=self._episode.connectors.calendar, mail=mail, tz=self._tz, workset=scratch
        )
        mails: dict[str, dict[str, Any]] = {}
        for handle in handles:  # reranker 입력용. 읽기는 Connector 호출이라 step으로 센다
            read = await reader.execute(READ_MAIL, {"handle": handle})
            if read.is_error:
                return ToolOutcome(_dump({"error": "read_failed"}), is_error=True)
            mails[handle] = json.loads(read.content)
        top: list[str] = []
        rerank_ms = 0.0
        if handles:
            passages = [rerank_passage(mails[h]) for h in handles]
            started = time.perf_counter()
            scores = await asyncio.to_thread(self._reranker.score, agent_query, passages)
            rerank_ms = round((time.perf_counter() - started) * 1000, 1)
            if len(scores) != len(passages):
                raise RuntimeError("reranker 점수 개수가 후보 수와 다르다")
            top = [handles[i] for i in rerank_order(scores, limit)]
        by_handle = {h["handle"]: h for h in hits}
        results: list[dict[str, Any]] = []
        shown: dict[str, dict[str, Any]] = {}  # 실제 핸들 -> 읽은 메일
        for h in top:
            entry = dict(by_handle[h])
            entry["handle"] = workset.message_handle(scratch.resolve_message(h))
            results.append(entry)
            shown[entry["handle"]] = mails[h]
        compress_fallback = 0
        empties = 0
        if advanced and results:
            boundary = boundary_for(f"a2x:{self._episode.task.task_id}:{len(self.calls)}")
            order = [e["handle"] for e in results]
            compressed, compress_fallback = await self._compress(
                self._episode, agent_query, order, shown, boundary
            )
            self.compress_fallback_mails += compress_fallback
            # A1과 같다: 원래 본문이 있었는데 압축 뒤 빈 메일은 머리글만 담아 돌려주고 센다
            empties = sum(
                1 for h in order if str(shown[h].get("body") or "").strip() and not compressed[h]
            )
            self.empty_compressed_mails += empties
            for entry in results:
                text, truncated = limit_external(compressed[entry["handle"]])
                entry["excerpt"], entry["excerpt_truncated"] = text, truncated
        self.calls.append(
            {
                "candidate_message_ids": [scratch.resolve_message(h) for h in handles],
                "returned_message_ids": [workset.resolve_message(e["handle"]) for e in results],
                "rerank_ms": rerank_ms,
                **(
                    {
                        "rewrite_fallback": rewrite_fallback,
                        "compress_fallback": compress_fallback,
                        "empty_compressed": empties,
                    }
                    if advanced
                    else {}
                ),
            }
        )
        return ToolOutcome(_dump({**data, "results": results}))

    def diag(self) -> dict[str, Any]:
        out: dict[str, Any] = {"search_variant": self._variant, "search_log": list(self.calls)}
        if self._variant == "advanced":
            out |= {
                "rewrite_fallback_calls": self.rewrite_fallback_calls,
                "compress_fallback_mails": self.compress_fallback_mails,
                "ignored_max_results": self.ignored_max_results,
                "empty_compressed_mails": self.empty_compressed_mails,
            }
        return out
