"""검색 방식 5개 (ADR-0028 §3). 모두 메일 ID 순위(상위 `TOP_N`)를 돌려준다."""

import uuid
from dataclasses import dataclass, field

from orbit.connectors.contracts import InvalidQuery, MailConnector
from orbit.connectors.sim.world import SimWorld
from orbit.db.session import AsyncDatabase
from orbit.evalrunner.retrieval.bm25 import Bm25Index
from orbit.evalrunner.retrieval.worldmail import mailbox_messages
from orbit.mailindex.chunking import index_text
from orbit.mailindex.embedding import Embedder
from orbit.mailindex.search import SearchMode, search_index
from orbit.mailindex.tokenize import tokenize

TOP_N = 20
METHODS = ("gmail_search", "keyword", "vector", "hybrid", "bm25")
_INDEX_MODES: dict[str, SearchMode] = {"keyword": "keyword", "vector": "vector", "hybrid": "hybrid"}


@dataclass
class MailboxSearcher:
    """메일함(크기) 하나에 대한 검색. 색인 워크스페이스·Sim 커넥터·BM25 말뭉치를 묶는다."""

    account_id: str
    workspace_id: uuid.UUID
    mail: MailConnector
    bm25: Bm25Index
    errors: dict[str, int] = field(default_factory=dict)


def build_bm25(world: SimWorld, account_id: str) -> Bm25Index:
    """색인되는 메일 전체를 문서로 한 BM25. 메일 한 통 = 문서 하나, 토큰은 운영과 같다."""
    docs = {
        m.id: tokenize(index_text(m.subject, m.body_text))
        for m in mailbox_messages(world, account_id)
    }
    return Bm25Index(docs)


async def rank(
    method: str,
    query: str,
    searcher: MailboxSearcher,
    db: AsyncDatabase,
    embedder: Embedder,
) -> list[str]:
    if method == "gmail_search":
        try:
            page = await searcher.mail.search_messages(query, max_results=TOP_N)
        except InvalidQuery:
            searcher.errors["gmail_search_invalid_query"] = (
                searcher.errors.get("gmail_search_invalid_query", 0) + 1
            )
            return []
        return [m.id for m in page.messages][:TOP_N]
    if method == "bm25":
        return [doc for doc, _ in searcher.bm25.search(tokenize(query), TOP_N)]
    mode = _INDEX_MODES.get(method)
    if mode is None:
        raise ValueError(f"알 수 없는 검색 방식: {method}")
    embedding = (await embedder.embed([query]))[0] if mode != "keyword" else None
    tokens = tokenize(query) if mode != "vector" else []
    async with db.workspace_session(searcher.workspace_id) as session:
        hits = await search_index(
            session,
            searcher.workspace_id,
            embedding=embedding,
            tokens=tokens,
            limit=TOP_N,
            mode=mode,
        )
    return [h.provider_item_id for h in hits]


def parse_methods(value: str) -> tuple[str, ...]:
    if value == "all":
        return METHODS
    names = tuple(dict.fromkeys(v.strip() for v in value.split(",") if v.strip()))
    unknown = [n for n in names if n not in METHODS]
    if unknown or not names:
        raise ValueError(f"알 수 없는 방식: {unknown or value}")
    return names
