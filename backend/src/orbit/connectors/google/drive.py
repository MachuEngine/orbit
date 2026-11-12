"""Drive Adapter (ADR-0020 §2, ADR-0008 5절). `DriveConnector` 계약을 Drive API v3 REST로 구현한다.

- 읽기 전용이다(`drive.readonly`). 쓰기·공유·링크 메서드는 없다. 응답 `fields`를 최소화하고
  링크 필드(`webViewLink` 등)는 요청하지 않는다(D6-1). 첨부·이미지·외부 링크를 읽지 않는다.
- 모델이 낸 문자열을 `q`에 그대로 쓰지 않는다. 어댑터가 구조화된 조건(`FileQuery`)으로 `q`를 만들고
  문자열 값의 역슬래시와 작은따옴표를 이스케이프한다. 휴지통(`trashed=false`)과 폴더는 늘 뺀다.
  텍스트 조건이 없으면 `orderBy=modifiedTime desc`(최근 수정순)이다. Drive는 `fullText` 질의에
  `orderBy`를 거절하므로(`Sorting is not supported for queries with fullText terms`) 텍스트
  조건이 있으면 `orderBy`를 빼고 Drive가 주는 순서(관련도순)를 그대로 쓴다.
- 본문 읽기: Google 문서는 `files.export`(`text/plain`), 스프레드시트는 `text/csv`(첫 시트),
  프레젠테이션은 `text/plain`. 일반 텍스트 파일은 `alt=media`. 그 밖의 형식, 10MB 초과, 내보내기
  한도 오류는 `UnsupportedFile`이다.
- 문서 권한이 없으면(403 `insufficientPermissions`·`ACCESS_TOKEN_SCOPE_INSUFFICIENT`) 사유 코드
  `scope_missing`인 `ConnectorError`다(`PermissionRevoked`로 보지 않는다). 404는 `NotFound`이고
  휴지통에 있는 파일도 `NotFound`로 본다.
- 오류 메시지에는 HTTP 상태와 사유 코드만 넣는다. 파일 이름·본문·주소·토큰은 넣지 않는다.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from orbit.connectors.contracts import (
    EXPORT_MIME_TYPES,
    FOLDER_MIME,
    KIND_MIME_TYPES,
    MAX_FILE_BYTES,
    MAX_FILE_PAGE_SIZE,
    Address,
    DriveFile,
    FilePage,
    FileQuery,
    FileText,
    NotFound,
    TransientError,
    UnsupportedFile,
    file_kind,
    is_readable_mime,
    normalize_mime,
)
from orbit.connectors.google._http import GoogleHttp, Sleep
from orbit.connectors.google.tokens import AccessTokenProvider

DEFAULT_BASE_URL = "https://www.googleapis.com/drive/v3"

_FILE_FIELDS = "id,name,mimeType,modifiedTime,owners(displayName,emailAddress),size,trashed"
_LIST_FIELDS = f"nextPageToken,files({_FILE_FIELDS})"


# ---------------------------------------------------------------- 검색 질의 변환


def escape_q(value: str) -> str:
    """Drive `q` 문자열 값의 `\\`와 `'`를 이스케이프한다(역슬래시를 먼저)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_drive_query(query: FileQuery) -> str:
    """구조화된 조건을 Drive `q`로 바꾼다. 값은 모두 이스케이프한 따옴표 문자열이나 열거 상수다."""
    parts = ["trashed = false", f"mimeType != '{FOLDER_MIME}'"]
    if query.text is not None:
        needle = escape_q(query.text.strip())
        parts.append(f"(name contains '{needle}' or fullText contains '{needle}')")
    if query.kind is not None:
        if query.kind == "other":
            parts += [f"mimeType != '{mime}'" for mime in KIND_MIME_TYPES.values()]
        else:
            parts.append(f"mimeType = '{KIND_MIME_TYPES[query.kind]}'")
    if query.modified_after is not None:
        parts.append(f"modifiedTime >= '{_rfc3339(query.modified_after)}'")
    if query.modified_before is not None:
        parts.append(f"modifiedTime < '{_rfc3339(query.modified_before)}'")
    return " and ".join(parts)


# ---------------------------------------------------------------- 응답 해석


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _parsed[T](fn: Callable[[Mapping[str, Any]], T], resource: Mapping[str, Any]) -> T:
    """읽기 경로 파싱 오류는 응답 내용을 담지 않고 `TransientError`로 감싼다."""
    try:
        return fn(resource)
    except Exception:
        raise TransientError("Drive 응답을 해석하지 못했다") from None


def _file(res: Mapping[str, Any]) -> DriveFile:
    mime = str(res["mimeType"])
    size = res.get("size")
    return DriveFile(
        id=str(res["id"]),
        name=str(res.get("name", "")),
        kind=file_kind(mime),
        mime_type=mime,
        modified_at=datetime.fromisoformat(str(res["modifiedTime"])),
        owners=tuple(
            Address(
                email=str(o["emailAddress"]),
                name=str(o["displayName"]) if o.get("displayName") else None,
            )
            for o in res.get("owners") or []
            if o.get("emailAddress")
        ),
        size_bytes=int(size) if size is not None else None,
    )


def _decode(raw: bytes, max_chars: int, meta: DriveFile) -> FileText:
    text = raw.decode("utf-8", errors="replace")
    return FileText(file=meta, text=text[:max_chars], truncated=len(text) > max_chars)


class GoogleDriveConnector:
    """`DriveConnector` 계약의 Drive 구현. 연결(계정) 하나에 묶인다.

    HTTP 규칙(토큰 갱신, 오류 대응, `base_url` 검증)은 `_http.GoogleHttp`가 Gmail·Calendar 어댑터와
    같이 쓰는 공통부다. 문서 권한이 방금 추가됐을 수 있으므로 호출자는 이 커넥터를 만들 때
    새 토큰 공급자를 줘야 한다(`GoogleConnectorResolver.drive_for`).
    """

    def __init__(
        self,
        tokens: AccessTokenProvider,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        allow_test_base_url: bool = False,
        timeout_s: float = 20.0,
        read_retries: int = 2,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = GoogleHttp(
            tokens,
            service="Drive",
            base_url=base_url,
            client=client,
            allow_test_base_url=allow_test_base_url,
            timeout_s=timeout_s,
            read_retries=read_retries,
            sleep=sleep,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search_files(
        self, query: FileQuery, *, max_results: int = 20, page_token: str | None = None
    ) -> FilePage:
        if not 1 <= max_results <= MAX_FILE_PAGE_SIZE:
            raise ValueError(f"max_results는 1~{MAX_FILE_PAGE_SIZE}이어야 한다")
        params = [
            ("q", to_drive_query(query)),
            ("pageSize", str(max_results)),
            ("fields", _LIST_FIELDS),
            ("spaces", "drive"),
            ("corpora", "user"),
        ]
        if query.text is None:
            params.append(("orderBy", "modifiedTime desc"))
        if page_token:
            params.append(("pageToken", page_token))
        data = await self._http.request(
            "GET", "/files", op="search_files", write=False, params=params, query=True, drive=True
        )

        def build(res: Mapping[str, Any]) -> FilePage:
            token = res.get("nextPageToken")
            return FilePage(
                files=tuple(_file(f) for f in res.get("files") or [] if not f.get("trashed")),
                next_page_token=str(token) if token else None,
            )

        return _parsed(build, data)

    async def get_file(self, file_id: str) -> DriveFile:
        data = await self._http.request(
            "GET",
            f"/files/{_quote(file_id)}",
            op="get_file",
            write=False,
            params={"fields": _FILE_FIELDS},
            drive=True,
        )
        if data.get("trashed") is True:
            raise NotFound("Drive get_file 실패: 휴지통에 있는 파일", reason="trashed")
        return _parsed(_file, data)

    async def read_file_text(self, file_id: str, *, max_chars: int) -> FileText:
        if max_chars < 1:
            raise ValueError("max_chars는 1 이상이어야 한다")
        meta = await self.get_file(file_id)
        if not is_readable_mime(meta.mime_type):
            raise UnsupportedFile("Drive read_file_text: 읽을 수 없는 형식", reason="unsupported")
        if meta.size_bytes is not None and meta.size_bytes > MAX_FILE_BYTES:
            raise UnsupportedFile("Drive read_file_text: 파일이 한도를 넘는다", reason="too_large")
        mime = normalize_mime(meta.mime_type)
        path = f"/files/{_quote(file_id)}"
        if mime in EXPORT_MIME_TYPES:
            raw = await self._http.request_bytes(
                "GET",
                f"{path}/export",
                op="export_file",
                params={"mimeType": EXPORT_MIME_TYPES[mime]},
                drive=True,
            )
        else:
            raw = await self._http.request_bytes(
                "GET", path, op="download_file", params={"alt": "media"}, drive=True
            )
        if len(raw) > MAX_FILE_BYTES:
            raise UnsupportedFile("Drive read_file_text: 파일이 한도를 넘는다", reason="too_large")
        return _decode(raw, max_chars, meta)
