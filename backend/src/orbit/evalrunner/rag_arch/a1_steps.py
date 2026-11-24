"""A1 계열 LLM 단계(ADR-0036·0038·0041): 질의 재작성과 문맥 압축.

`RagArchSUT`(A1·A2X)와 `RunflowV0SUT`(`--search-variant advanced`)가 같은 코드를 쓴다.
프롬프트·버전·파라미터·fallback 규칙을 바꾸면 `config.py`의 버전을 함께 올린다.
"""

import json
from typing import Any

from orbit.assistant.agent import TEMPERATURE
from orbit.assistant.envelope import wrap_data
from orbit.assistant.prompt import data_rule_lines
from orbit.evalrunner.episode import Episode
from orbit.evalrunner.rag_arch.a1 import compress_body, parse_compression
from orbit.evalrunner.rag_arch.config import (
    A1_COMPRESS_PROMPT,
    A1_REWRITE_PROMPT,
    COMPRESS_MAX_OUTPUT_TOKENS,
    REWRITE_MAX_OUTPUT_TOKENS,
)
from orbit.llm.types import LLMRequest, Message
from orbit.mailindex.limits import MAX_QUERY_CHARS
from orbit.runflow.rag import parsed_query


async def rewrite_query(episode: Episode, question: str) -> tuple[str, bool]:
    """A1 질의 재작성(LLM 1회). (질의, fallback 여부). 형식이 틀리면 입력 질문을 쓴다."""
    episode.steps.consume()
    rewritten = await episode.llm.complete(
        LLMRequest(
            messages=(
                Message(role="system", content=A1_REWRITE_PROMPT),
                Message(role="user", content=question),
            ),
            purpose="system_under_test",
            max_output_tokens=REWRITE_MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
            metadata={"task_id": episode.task.task_id, "step_kind": "a1_rewrite"},
        )
    )
    query, fallback = parsed_query(rewritten.text, question)
    if rewritten.tool_calls or rewritten.finish_reason == "length":
        query, fallback = question[:MAX_QUERY_CHARS], True
    return query, fallback


async def compress_mails(
    episode: Episode,
    question: str,
    handles: list[str],
    mails: dict[str, dict[str, Any]],
    boundary: str,
) -> tuple[dict[str, str], int]:
    """LLM 1회로 5통을 한 번에 압축한다. 검증을 통과하지 못한 메일은 원문 본문으로 되돌린다."""
    episode.steps.consume()
    blocks = [
        wrap_data(
            json.dumps({"handle": h, "body": mails[h].get("body", "")}, ensure_ascii=False),
            source="mail_body",
            boundary=boundary,
        )
        for h in handles
    ]
    system = "\n".join([A1_COMPRESS_PROMPT, "", *data_rule_lines(boundary)])
    response = await episode.llm.complete(
        LLMRequest(
            messages=(
                Message(role="system", content=system),
                Message(role="user", content="\n\n".join([question, *blocks])),
            ),
            purpose="system_under_test",
            max_output_tokens=COMPRESS_MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
            metadata={"task_id": episode.task.task_id, "step_kind": "a1_compress"},
        )
    )
    parsed = None
    if not response.tool_calls and response.finish_reason != "length":
        parsed = parse_compression(response.text)
    out: dict[str, str] = {}
    fallbacks = 0
    for h in handles:
        original = str(mails[h].get("body", ""))
        picked = compress_body(original, parsed[h]) if parsed is not None and h in parsed else None
        if picked is None:
            fallbacks += 1
        out[h] = original if picked is None else picked
    return out, fallbacks
