"""평가 전용 턴 시작 검색 정책(ADR-0035). 제품 경로에서는 주입하지 않는다."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orbit.llm.interface import LLM
from orbit.llm.types import LLMRequest, Message
from orbit.runflow.transcript import Transcript

AGENTIC = "agentic"
RAG_PATTERNS = (AGENTIC, "retrieve_once", "rewrite_retrieve_once")
RAG_VERSION = "rag-once-v1"
REWRITE_VERSION = "rag-rewrite-v1"
MAX_QUERY_CHARS = 500
REWRITE_MAX_OUTPUT_TOKENS = 256
INITIAL_STEP = -1  # RLS transcript 전용. 그래프의 LLM step 0은 그대로 둔다.
REWRITE_PROMPT = (
    "사용자의 현재 질문을 메일 내용 검색 질의로 바꾼다. 사용자 대화만 참고하고 "
    "답을 만들거나 도구를 호출하지 않는다. 핵심 이름·주제·날짜를 유지한다. "
    'JSON 객체 {"query":"검색 질의"}만 출력한다. query는 1~500자다.'
)


def initial_call_id(run_id: str) -> str:
    """같은 run 재진입은 같고, 턴이 다르면 다른 서버 호출 ID다."""
    return f"orbit_rag_{run_id.replace('-', '')}"


def parsed_query(text: str | None, fallback: str) -> tuple[str, bool]:
    fallback = fallback[:MAX_QUERY_CHARS]
    try:
        data = json.loads(text or "")
    except ValueError:
        return fallback, True
    query = data.get("query") if isinstance(data, dict) and set(data) == {"query"} else None
    if not isinstance(query, str) or not query.strip() or len(query.strip()) > MAX_QUERY_CHARS:
        return fallback, True
    return query.strip(), False


@dataclass
class RagPolicy:
    """콘텐츠는 transcript에만 저장한다. 여기는 집계용 제어 값만 둔다."""

    pattern: str
    before_search: Callable[[], None] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.pattern not in RAG_PATTERNS[1:]:
            raise ValueError("평가 RAG 정책이 올바르지 않다")

    async def query(self, transcript: Transcript, llm: LLM) -> str:
        question = next(
            str(r.content.get("text", "")) for r in reversed(transcript.rows) if r.role == "user"
        )
        if self.pattern == "retrieve_once":
            if len(question) > MAX_QUERY_CHARS:
                self.steps.append({"kind": "rag_query_truncated", "source": "current_question"})
            return question[:MAX_QUERY_CHARS]
        try:
            response = await llm.complete(
                LLMRequest(
                    messages=(
                        Message("system", REWRITE_PROMPT),
                        *(
                            Message(
                                "user" if r.role == "user" else "assistant", str(r.content["text"])
                            )
                            for r in (*transcript.prior.rows, *transcript.rows)
                            if r.role in ("user", "assistant") and r.content.get("text")
                        ),
                    ),
                    purpose="system_under_test",
                    max_output_tokens=REWRITE_MAX_OUTPUT_TOKENS,
                    temperature=0.0,
                    metadata={"step_kind": "rag_rewrite", "prompt_version": REWRITE_VERSION},
                )
            )
        except BaseException as exc:
            self.steps.append({"kind": "rag_rewrite", "error_kind": type(exc).__name__})
            raise
        query, fallback = parsed_query(response.text, question)
        if response.tool_calls or response.finish_reason == "length":
            query, fallback = question[:MAX_QUERY_CHARS], True
        self.steps.append({"kind": "rag_rewrite", "fallback": fallback})
        if fallback and len(question) > MAX_QUERY_CHARS:
            self.steps.append({"kind": "rag_query_truncated", "source": "current_question"})
        return query


def fingerprint_payload(pattern: str) -> dict[str, Any]:
    return {
        "pattern": pattern,
        "version": RAG_VERSION,
        "top_k": 5,
        "rewrite_version": REWRITE_VERSION,
        "rewrite_prompt": REWRITE_PROMPT,
        "rewrite_max_output_tokens": REWRITE_MAX_OUTPUT_TOKENS,
        "max_query_chars": MAX_QUERY_CHARS,
    }
