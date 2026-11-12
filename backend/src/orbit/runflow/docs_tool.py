"""문서 도구 `search_docs`·`read_doc` (ADR-0020 §3). 실사용 경로(`runflow`)에서만 쓴다.

- 읽기 도구다. 결과 파일은 작업 집합에 `D` 핸들로 등록되고 제공자 ID는 결과에 넣지 않는다.
  모델은 Drive 검색 문법을 쓰지 않고 구조화된 조건(`text`·`kind`·수정일)만 낸다.
- 이름·소유자 이름은 외부 콘텐츠이므로 길이 상한을 둔다(`MAX_FIELD_CHARS`). 본문은 `limit_external`
  상한에서 자르고 `body_truncated`로 표시한다. 데이터 봉투는 LLM 메시지를 만들 때 씌운다(ADR-0018).
- 링크는 돌려주지 않고 열지 않는다(D6-1). 문서 권한이 없으면 `scope_missing`을 오류 종류로 돌려준다.
- 인자 검증 실패와 커넥터 오류는 `{"error": 종류}`다. 원문 메시지는 넣지 않는다.
"""

import json
from datetime import datetime, tzinfo
from typing import Any

from pydantic import ValidationError

from orbit.assistant.tools import (
    MAX_EXTERNAL_CHARS,
    MAX_FIELD_CHARS,
    MAX_SEARCH_RESULTS,
    ToolInputError,
    ToolOutcome,
    limit_external,
)
from orbit.assistant.workset import UnknownHandleError, WorkSet
from orbit.connectors.contracts import (
    FILE_KINDS,
    Address,
    ConnectorError,
    DriveConnector,
    DriveFile,
    FileQuery,
)
from orbit.connectors.errors import connector_error_kind
from orbit.llm.types import ToolSpec

SEARCH_DOCS = "search_docs"
READ_DOC = "read_doc"
DOC_TOOLS = frozenset({SEARCH_DOCS, READ_DOC})

DEFAULT_DOC_RESULTS = 10
MIN_READ_CHARS = 200


def doc_tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            SEARCH_DOCS,
            "Drive 문서를 이름·내용으로 검색해 최근 수정순 목록을 돌려준다. 문서마다 이름, 종류, "
            "수정 시각, 소유자와 핸들(D1 ...)이 있다. 조건은 모두 선택이며 AND로 적용된다. "
            "휴지통과 폴더는 제외한다. 검색 문법(OR, 연산자)은 쓰지 않고 text에는 찾을 낱말만 "
            "적는다. 문서 권한이 없으면 error가 scope_missing이다.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "이름이나 내용에 들어 있는 글"},
                    "kind": {"type": "string", "enum": list(FILE_KINDS)},
                    "modified_after": {
                        "type": "string",
                        "description": "이 시각 이후 수정(포함), 오프셋 필수",
                    },
                    "modified_before": {
                        "type": "string",
                        "description": "이 시각 이전 수정(미포함), 오프셋 필수",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_RESULTS,
                        "description": f"기본 {DEFAULT_DOC_RESULTS}",
                    },
                },
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            READ_DOC,
            "문서 하나의 본문 텍스트를 읽는다. Google 문서·프레젠테이션은 텍스트, 스프레드시트는 "
            "첫 시트 CSV, 일반 텍스트 파일은 원문이다. 그 밖의 형식은 unsupported_file 오류다. "
            "body_truncated가 true면 앞부분만 읽은 것이다.",
            {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "문서 핸들(예: D1)"},
                    "max_chars": {
                        "type": "integer",
                        "minimum": MIN_READ_CHARS,
                        "maximum": MAX_EXTERNAL_CHARS,
                        "description": f"읽을 최대 글자 수, 기본 {MAX_EXTERNAL_CHARS}",
                    },
                },
                "required": ["handle"],
                "additionalProperties": False,
            },
        ),
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _short(text: str | None) -> str | None:
    return None if text is None else text[:MAX_FIELD_CHARS]


def _person(p: Address) -> dict[str, str | None]:
    return {"name": _short(p.name), "email": _short(p.email)}


def _aware(value: object) -> datetime:
    if not isinstance(value, str):
        raise ToolInputError("invalid_time")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ToolInputError("invalid_time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolInputError("invalid_time")
    return parsed


def _kind_of(exc: Exception) -> str:
    if isinstance(exc, ToolInputError):
        return exc.kind
    if isinstance(exc, UnknownHandleError):
        return "unknown_handle"
    if isinstance(exc, ConnectorError):
        return connector_error_kind(exc)
    return "invalid_arguments"


class DocTools:
    """한 번의 도구 실행에 쓰는 문서 도구 실행기. 작업 집합과 커넥터는 호출자가 준다."""

    def __init__(self, drive: DriveConnector, workset: WorkSet, tz: tzinfo) -> None:
        self._drive = drive
        self._workset = workset
        self._tz = tz

    async def execute(self, name: str, arguments: object) -> ToolOutcome:
        try:
            if not isinstance(arguments, dict):
                raise ToolInputError("invalid_arguments")
            if name == SEARCH_DOCS:
                return ToolOutcome(_dump(await self._search(arguments)))
            if name == READ_DOC:
                return ToolOutcome(_dump(await self._read(arguments)))
            raise ToolInputError("unknown_tool")
        except (ToolInputError, UnknownHandleError, ConnectorError) as exc:
            kind = _kind_of(exc)
            info = (kind, exc.reason) if isinstance(exc, ConnectorError) else None
            return ToolOutcome(_dump({"error": kind}), is_error=True, connector_error=info)

    def _local(self, moment: datetime) -> str:
        return moment.astimezone(self._tz).isoformat()

    def _summary(self, file: DriveFile) -> dict[str, Any]:
        return {
            "handle": self._workset.document_handle(file.id),
            "name": _short(file.name),
            "kind": file.kind,
            "modified": self._local(file.modified_at),
            "owners": [_person(o) for o in file.owners],
        }

    async def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {"text", "kind", "modified_after", "modified_before", "max_results"}
        if set(args) - allowed:
            raise ToolInputError("invalid_arguments")
        limit = args.get("max_results", DEFAULT_DOC_RESULTS)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ToolInputError("invalid_arguments")
        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ToolInputError("invalid_arguments")
        text, kind = args.get("text"), args.get("kind")
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise ToolInputError("invalid_arguments")
        if kind is not None and kind not in FILE_KINDS:
            raise ToolInputError("invalid_arguments")
        after = _aware(args["modified_after"]) if args.get("modified_after") else None
        before = _aware(args["modified_before"]) if args.get("modified_before") else None
        try:
            query = FileQuery(text=text, kind=kind, modified_after=after, modified_before=before)
        except ValidationError:
            raise ToolInputError("invalid_arguments") from None
        page = await self._drive.search_files(query, max_results=limit)
        return {
            "files": [self._summary(f) for f in page.files],
            "has_more": page.next_page_token is not None,
        }

    async def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        if "handle" not in args or set(args) - {"handle", "max_chars"}:
            raise ToolInputError("invalid_arguments")
        max_chars = args.get("max_chars", MAX_EXTERNAL_CHARS)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise ToolInputError("invalid_arguments")
        if not MIN_READ_CHARS <= max_chars <= MAX_EXTERNAL_CHARS:
            raise ToolInputError("invalid_arguments")
        file_id = self._workset.resolve_document(args["handle"])
        content = await self._drive.read_file_text(file_id, max_chars=max_chars)
        body, cut = limit_external(content.text)
        summary = self._summary(content.file)
        summary["body"] = body
        summary["body_truncated"] = content.truncated or cut
        return summary
