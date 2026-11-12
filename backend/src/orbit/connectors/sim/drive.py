"""Sim 문서(Drive) 커넥터 (ADR-0020 §2). 모든 메서드는 `SimRuntime.run_tool`을 거친다.

Google 어댑터와 같은 의미로 동작한다: 휴지통·폴더 제외, 종류 필터,
수정일 이후(이상)·이전(미만). 텍스트 조건이 없으면 최근 수정순(같으면 ID순), 있으면 관련도순의
근사(이름 일치가 본문만 일치보다 앞, 같은 등급은 최근 수정순)다. 읽기는 Google 문서/스프레드시트/
프레젠테이션과 일반 텍스트만 가능하다.

계약 아님 (Sim 고유 동작, 호출자가 의존하면 안 된다):
- `text` 조건은 이름·본문의 대소문자 무시 부분 문자열 일치다(Drive는 단어 단위).
- 페이지 토큰은 정렬된 결과의 오프셋(`o:<n>`)이라 결정적이다.
- 문서 권한 부족은 장애 주입(`scope_missing`)으로만 재현한다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from orbit.connectors.contracts import (
    DRIVE_TOOLS,
    FOLDER_MIME,
    MAX_FILE_BYTES,
    MAX_FILE_PAGE_SIZE,
    DriveFile,
    FilePage,
    FileQuery,
    FileText,
    InvalidQuery,
    NotFound,
    UnsupportedFile,
    file_kind,
    is_readable_mime,
)
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import AccountState, SimFile, SimWorld

_CURSOR_PREFIX = "o:"


def _size(file: SimFile) -> int:
    return file.size_bytes if file.size_bytes is not None else len(file.text.encode("utf-8"))


def _meta(file: SimFile) -> DriveFile:
    return DriveFile(
        id=file.id,
        name=file.name,
        kind=file_kind(file.mime_type),
        mime_type=file.mime_type,
        modified_at=file.modified_at,
        owners=file.owners,
        size_bytes=_size(file),
    )


class SimDriveConnector:
    def __init__(self, world: SimWorld, account_id: str, runtime: SimRuntime) -> None:
        self._world = world
        self._account_id = account_id
        self._rt = runtime
        self._world.account(account_id)  # 없는 계정이면 바로 실패한다
        self._rt.bind(DRIVE_TOOLS)

    def _run[T](
        self, tool: str, args: Mapping[str, Any], apply_fn: Callable[[], T]
    ) -> Awaitable[T]:
        return self._rt.run_tool(tool, args, apply_fn, account_id=self._account_id)

    @property
    def _state(self) -> AccountState:
        return self._world.account(self._account_id)

    def _get(self, file_id: str) -> SimFile:
        file = self._state.files.get(file_id)
        if file is None or file.trashed:
            raise NotFound(f"파일 {file_id}가 없다")
        return file

    async def search_files(
        self, query: FileQuery, *, max_results: int = 20, page_token: str | None = None
    ) -> FilePage:
        args = {"query": query, "max_results": max_results, "page_token": page_token}

        def apply() -> FilePage:
            if not 1 <= max_results <= MAX_FILE_PAGE_SIZE:
                raise ValueError(f"max_results는 1~{MAX_FILE_PAGE_SIZE}이어야 한다")
            offset = _offset(page_token)
            needle = query.text.casefold() if query.text is not None else None
            found = []
            for file in self._state.files.values():
                if file.trashed or file.mime_type == FOLDER_MIME:
                    continue
                if query.kind is not None and file_kind(file.mime_type) != query.kind:
                    continue
                if query.modified_after is not None and file.modified_at < query.modified_after:
                    continue
                if query.modified_before is not None and file.modified_at >= query.modified_before:
                    continue
                if (
                    needle is not None
                    and needle not in file.name.casefold()
                    and needle not in file.text.casefold()
                ):
                    continue
                found.append(file)
            if needle is None:
                found.sort(key=lambda f: (-f.modified_at.timestamp(), f.id))
            else:  # 관련도 근사: 이름 일치 먼저, 그 안에서 최근 수정순
                found.sort(
                    key=lambda f: (
                        needle not in f.name.casefold(),
                        -f.modified_at.timestamp(),
                        f.id,
                    )
                )
            page = found[offset : offset + max_results]
            more = offset + max_results < len(found)
            return FilePage(
                files=tuple(_meta(f) for f in page),
                next_page_token=f"{_CURSOR_PREFIX}{offset + max_results}" if more else None,
            )

        return await self._run("search_files", args, apply)

    async def get_file(self, file_id: str) -> DriveFile:
        return await self._run("get_file", {"file_id": file_id}, lambda: _meta(self._get(file_id)))

    async def read_file_text(self, file_id: str, *, max_chars: int) -> FileText:
        args = {"file_id": file_id, "max_chars": max_chars}

        def apply() -> FileText:
            if max_chars < 1:
                raise ValueError("max_chars는 1 이상이어야 한다")
            file = self._get(file_id)
            if not is_readable_mime(file.mime_type):
                raise UnsupportedFile(f"파일 {file_id}는 읽을 수 없는 형식이다")
            if _size(file) > MAX_FILE_BYTES:
                raise UnsupportedFile(f"파일 {file_id}가 한도({MAX_FILE_BYTES}바이트)를 넘는다")
            return FileText(
                file=_meta(file),
                text=file.text[:max_chars],
                truncated=len(file.text) > max_chars,
            )

        return await self._run("read_file_text", args, apply)


def _offset(page_token: str | None) -> int:
    if page_token is None:
        return 0
    if not page_token.startswith(_CURSOR_PREFIX) or not page_token[2:].isdigit():
        raise InvalidQuery("잘못된 페이지 토큰")
    return int(page_token[len(_CURSOR_PREFIX) :])
