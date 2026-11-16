"""임베딩 비용 추정 (ADR-0028 §6). 호출도 DB 접근도 하지 않는다.

메일함 순서대로 색인할 때 새로 임베딩할 청크(캐시에 없고 앞 메일함에서 이미 센 것이 아닌 것)의
수·추정 토큰·추정 비용을 계산한다. 토큰은 글자 수 기준 보수적 추정이다(`EST_TOKENS_PER_CHAR`).
이미 지문이 같은 색인이 평가 DB에 있으면 임베딩이 필요 없지만 여기서는 DB를 보지 않으므로 반영하지
않는다(그 경우 실제 비용은 추정보다 작다).
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orbit.connectors.sim.world import SimWorld
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.embed_cache import (
    EST_TOKENS_PER_CHAR,
    PRICE_PER_MTOK_USD,
    estimate_cost_usd,
    estimate_tokens,
    read_cached_keys,
    text_key,
)


def dry_run_report(
    *,
    world: SimWorld,
    sizes: Sequence[str],
    accounts: dict[str, str],
    query_texts: Sequence[str],
    model_id: str,
    cache_dir: Path,
    price_per_mtok: float | None,
    needs_query_embedding: bool,
) -> dict[str, Any]:
    in_file = read_cached_keys(cache_dir, model_id)
    seen = set(in_file)
    cached_total = len(in_file)
    per_account: dict[str, Any] = {}
    total_tokens = 0
    total_chunks = 0
    total_new = 0
    for size in sizes:
        account = accounts[size]
        mails = worldmail.mailbox_messages(world, account)
        chunks = worldmail.mailbox_chunks(world, account)
        unique = dict.fromkeys(text_key(c) for c in chunks)
        by_key = {text_key(c): c for c in chunks}
        cached_here = sum(1 for k in unique if k in in_file)
        new_keys = [k for k in unique if k not in seen]
        tokens = sum(estimate_tokens(by_key[k]) for k in new_keys)
        seen.update(new_keys)
        per_account[size] = {
            "account_id": account,
            "mails": len(mails),
            "chunks": len(chunks),
            "unique_chunks": len(unique),
            "cached_in_file": cached_here,
            "shared_with_earlier_accounts": len(unique) - len(new_keys) - cached_here,
            "new_chunks": len(new_keys),
            "est_tokens": tokens,
            "est_cost_usd": estimate_cost_usd(model_id, tokens, price_per_mtok),
        }
        total_tokens += tokens
        total_chunks += len(chunks)
        total_new += len(new_keys)
    query_new = (
        {t for t in query_texts if text_key(t) not in seen} if needs_query_embedding else set()
    )
    query_tokens = sum(estimate_tokens(t) for t in query_new)
    grand = total_tokens + query_tokens
    return {
        "dry_run": True,
        "calls_made": 0,
        "embedding_model_id": model_id,
        "price_per_mtok_usd": price_per_mtok
        if price_per_mtok is not None
        else PRICE_PER_MTOK_USD.get(model_id),
        "est_tokens_per_char": EST_TOKENS_PER_CHAR,
        "cache_file_entries": cached_total,
        "accounts": per_account,
        "queries": {
            "texts": len(query_texts),
            "new": len(query_new),
            "est_tokens": query_tokens,
        },
        "total": {
            "chunks": total_chunks,
            "new_chunks": total_new,
            "est_tokens": grand,
            "est_cost_usd": estimate_cost_usd(model_id, grand, price_per_mtok),
        },
    }
