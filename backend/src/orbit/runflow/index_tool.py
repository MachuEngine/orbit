"""색인 메일 검색 도구 `search_indexed_mail` (ADR-0019 §8). 실사용 경로(`runflow`)에서만 쓴다.

- 읽기 도구다. 결과 메일은 작업 집합에 M 핸들로 등록돼 `read_mail`로 이어 읽을 수 있고, 제공자 ID는
  결과에 넣지 않는다. 제목·보낸 사람·날짜·일치 청크 발췌는 외부 콘텐츠이므로 길이 상한을 두고
  (`limit_external`, 필드 상한) 데이터 봉투는 LLM 메시지를 만들 때 씌운다(ADR-0018).
- 검색 직전에 마지막 동기화가 오래됐으면 시간 상한 안에서 증분 동기화를 먼저 한다. 실패·초과면 있는
  색인으로 검색한다.
- 색인 범위가 없으면 `index_empty`, 첫 동기화 전이거나 아직 읽지 않은 새 범위가 있으면
  `index_pending`을 함께 돌려준다.
- 질의문은 임베딩 호출과 검색 SQL의 바인드 인자로만 쓰고 저장·로그에 남기지 않는다.
"""

import json
import logging
import uuid
from datetime import datetime, tzinfo
from typing import Any

from orbit.assistant.tools import MAX_FIELD_CHARS, ToolOutcome, limit_external
from orbit.assistant.workset import WorkSet
from orbit.connectors.contracts import MailConnector
from orbit.llm.types import ToolSpec
from orbit.mailindex import store
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import EmbeddingError
from orbit.mailindex.limits import DEFAULT_RESULTS, MAX_QUERY_CHARS, MAX_RESULTS
from orbit.mailindex.search import SearchHit, search_index
from orbit.mailindex.sync import sync_before_query

logger = logging.getLogger(__name__)

SEARCH_INDEXED_MAIL = "search_indexed_mail"


def index_tool_spec() -> ToolSpec:
    return ToolSpec(
        SEARCH_INDEXED_MAIL,
        "사용자가 색인하도록 고른 메일을 내용·주제로 검색한다(의미와 키워드를 함께 쓴다). 메일마다 "
        "제목, 보낸 사람, 날짜, 가장 일치한 본문 조각(excerpt)과 핸들을 돌려준다. 결과의 "
        "indexed_as_of는 색인이 마지막으로 갱신된 시각이고 indexed_minutes_ago는 그 뒤 흐른 "
        "분이다. 색인 범위가 없으면 index_empty가 true다. 날짜·보낸 사람처럼 조건이 분명하면 "
        "search_mail을 쓴다.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "찾을 내용이나 주제를 문장이나 키워드로",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULTS,
                    "description": f"기본 {DEFAULT_RESULTS}",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _short(text: str | None) -> str | None:
    return None if text is None else text[:MAX_FIELD_CHARS]


class IndexedMailSearch:
    """한 워커의 색인 검색 실행기. 상태를 갖지 않으므로 run마다 새로 만들지 않아도 된다."""

    def __init__(self, mi: MailIndexDeps, tz: tzinfo) -> None:
        self._mi = mi
        self._tz = tz

    @staticmethod
    def _parse(arguments: object) -> tuple[str, int] | None:
        if not isinstance(arguments, dict) or "query" not in arguments:
            return None
        if set(arguments) - {"query", "max_results"}:
            return None
        query = arguments["query"]
        limit = arguments.get("max_results", DEFAULT_RESULTS)
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            return None
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
            return None
        return query.strip(), limit

    def _local(self, moment: datetime) -> str:
        return moment.astimezone(self._tz).isoformat()

    async def execute(
        self,
        arguments: object,
        *,
        workspace_id: uuid.UUID,
        workset: WorkSet,
        mail: MailConnector,
    ) -> ToolOutcome:
        parsed = self._parse(arguments)
        if parsed is None:
            return ToolOutcome(_dump({"error": "invalid_arguments"}), is_error=True)
        query, limit = parsed
        mi = self._mi
        ws = workspace_id

        async with mi.db.workspace_session(ws) as s:
            connection_id = await store.find_active_connection(s, ws)
            scopes = await store.list_scopes(s, ws, connection_id) if connection_id else []
            state = await store.get_sync_state(s, ws, connection_id) if connection_id else None
        if connection_id is None or not scopes:
            return ToolOutcome(_dump({"index_empty": True, "results": []}))

        await sync_before_query(
            mi, mail, ws, connection_id, None if state is None else state.last_synced_at
        )

        embedding: list[float] | None = None
        try:
            embedding = (await mi.embedder.embed([query]))[0]
        except EmbeddingError as exc:
            logger.warning("질의 임베딩 실패, 키워드만 쓴다: kind=%s", exc.kind)
        tokens = mi.tokenize(query)
        if embedding is None and not tokens:
            return ToolOutcome(_dump({"error": "index_unavailable"}), is_error=True)

        async with mi.db.workspace_session(ws) as s:
            hits = await search_index(s, ws, embedding=embedding, tokens=tokens, limit=limit)
            state = await store.get_sync_state(s, ws, connection_id)
            scopes = await store.list_scopes(s, ws, connection_id)

        synced_at = None if state is None else state.last_synced_at
        content: dict[str, Any] = {
            "results": [self._result(h, workset) for h in hits],
            "indexed_as_of": None if synced_at is None else self._local(synced_at),
            "indexed_minutes_ago": None
            if synced_at is None
            else max(0, int((mi.clock.now() - synced_at).total_seconds() // 60)),
        }
        # 첫 동기화 전이거나 아직 읽지 않은 새 범위가 있으면 결과가 일부일 수 있다.
        if synced_at is None or any(sc.initial_synced_at is None for sc in scopes):
            content["index_pending"] = True
        return ToolOutcome(_dump(content))

    def _result(self, hit: SearchHit, workset: WorkSet) -> dict[str, Any]:
        excerpt, truncated = limit_external(hit.excerpt)
        return {
            "handle": workset.message_handle(hit.provider_item_id),
            "subject": _short(hit.subject),
            "from": {"name": _short(hit.sender_name), "email": _short(hit.sender_email)},
            "date": self._local(hit.sent_at),
            "excerpt": excerpt,
            "excerpt_truncated": truncated,
        }
