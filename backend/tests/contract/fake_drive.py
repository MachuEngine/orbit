"""상태가 있는 모의 Drive API v3 서버 (`httpx.MockTransport` 핸들러).

`GoogleDriveConnector` 계약 테스트용이다. 구현 범위는 `files.list`(`q`, `orderBy`, `pageSize`,
`pageToken`), `files.get`, `files.get?alt=media`, `files.export`뿐이고 나머지는 404다. 어댑터 코드를
import하지 않고 Drive 리소스 모양(JSON)과 `q` 문법만 따른다.

- `q`는 이 파일의 작은 파서가 직접 해석한다: `and`, `or`, 괄호, `trashed = false`,
  `mimeType =|!= '값'`, `modifiedTime >=|< '시각'`, `name|fullText contains '값'`.
  문자열 값의 역슬래시와 작은따옴표 이스케이프를 푼다. 그 밖의 문법이나 따옴표가 어긋난
  질의는 400이다(이스케이프 누락을 잡는다).
- `contains`는 단어 단위(앞부분 일치)이고 Sim의 부분 문자열 일치보다 엄격하다.
- `orderBy=modifiedTime desc`일 때만 정렬한다(없으면 저장 순서). 실제 Drive처럼 `fullText`가 든
  `q`에 `orderBy`를 주면 400(`Sorting is not supported for queries with fullText terms`)이다.
- Google 문서류는 `size`를 돌려주지 않는다. 내보내기 한도 초과는 403 `exportSizeLimitExceeded`다.
- `scope_missing=True`면 모든 요청이 403 `insufficientPermissions`
  (+ `ACCESS_TOKEN_SCOPE_INSUFFICIENT`)다.
- `calls`에 (메서드, 경로, 쿼리 매개변수)를 남긴다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import unquote

import httpx

from orbit.connectors.sim.world import AccountState, SimFile

_WORD = re.compile(r"\w+", re.UNICODE)
_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:\\.|[^'\\])*')|(?P<op>>=|<=|!=|=|<|>|\(|\))|(?P<word>[A-Za-z]+))"
)
GOOGLE_NATIVE = (
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
)
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
LIMIT = 10 * 1024 * 1024


class BadQuery(ValueError):
    pass


def _error(status: int, reason: str, *, detail: str | None = None) -> httpx.Response:
    err: dict[str, Any] = {
        "code": status,
        "message": "fake",
        "errors": [{"reason": reason}],
    }
    if detail:
        err["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": detail}]
    return httpx.Response(status, json={"error": err})


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _contains(haystack: str, needle: str, *, prefix: bool) -> bool:
    have, want = _words(haystack), _words(needle)
    if not want:
        return False
    return all(any(h.startswith(w) if prefix else h == w for h in have) for w in want)


def _unescape(literal: str) -> str:
    return re.sub(r"\\(.)", r"\1", literal[1:-1])


def _tokens(q: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(q):
        if not q[pos:].strip():
            break
        m = _TOKEN.match(q, pos)
        if m is None:
            raise BadQuery
        kind = m.lastgroup or ""
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


Predicate = Callable[[SimFile], bool]


class _Parser:
    def __init__(self, q: str) -> None:
        self._t = _tokens(q)
        self._i = 0

    def _peek(self) -> tuple[str, str] | None:
        return self._t[self._i] if self._i < len(self._t) else None

    def _take(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise BadQuery
        self._i += 1
        return tok

    def parse(self) -> Predicate:
        pred = self._or()
        if self._peek() is not None:
            raise BadQuery
        return pred

    def _or(self) -> Predicate:
        left = self._and()
        while self._peek() == ("word", "or"):
            self._take()
            right = self._and()
            left = (lambda a, b: lambda f: a(f) or b(f))(left, right)
        return left

    def _and(self) -> Predicate:
        left = self._atom()
        while self._peek() == ("word", "and"):
            self._take()
            right = self._atom()
            left = (lambda a, b: lambda f: a(f) and b(f))(left, right)
        return left

    def _atom(self) -> Predicate:
        kind, value = self._take()
        if (kind, value) == ("op", "("):
            inner = self._or()
            if self._take() != ("op", ")"):
                raise BadQuery
            return inner
        if kind != "word":
            raise BadQuery
        if value == "trashed":
            if self._take() != ("op", "=") or self._take() != ("word", "false"):
                raise BadQuery
            return lambda f: not f.trashed
        field = value
        op_kind, op = self._take()
        if field in ("name", "fullText"):
            if (op_kind, op) != ("word", "contains"):
                raise BadQuery
            lit = self._literal()
            if field == "name":
                return lambda f: _contains(f.name, lit, prefix=True)
            return lambda f: _contains(f.text, lit, prefix=False)
        if op_kind != "op":
            raise BadQuery
        lit = self._literal()
        if field == "mimeType" and op in ("=", "!="):
            return (lambda f: f.mime_type == lit) if op == "=" else (lambda f: f.mime_type != lit)
        if field == "modifiedTime" and op in (">=", "<"):
            moment = datetime.fromisoformat(lit.replace("Z", "+00:00"))
            if op == ">=":
                return lambda f: f.modified_at >= moment
            return lambda f: f.modified_at < moment
        raise BadQuery

    def _literal(self) -> str:
        kind, value = self._take()
        if kind != "str":
            raise BadQuery
        return _unescape(value)


class FakeDrive:
    def __init__(self, account: AccountState) -> None:
        self.files: dict[str, SimFile] = dict(account.files)
        self.valid_token = "drive-mock-token"  # noqa: S105 - 테스트용 가짜 값
        self.scope_missing = False
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.raw_paths: list[bytes] = []  # 퍼센트 인코딩 그대로의 경로(쿼리 제외)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.calls.append((request.method, request.url.path, params))
        self.raw_paths.append(request.url.raw_path.split(b"?")[0])
        if request.headers.get("Authorization") != f"Bearer {self.valid_token}":
            return _error(401, "authError")
        if self.scope_missing:
            return _error(403, "insufficientPermissions", detail="ACCESS_TOKEN_SCOPE_INSUFFICIENT")
        path = request.url.path.removeprefix("/drive/v3")
        if request.method != "GET":
            return _error(404, "notFound")
        if path == "/files":
            return self._list(params)
        match = re.fullmatch(r"/files/([^/]+)(/export)?", path)
        if match is None:
            return _error(404, "notFound")
        file = self.files.get(unquote(match.group(1)))
        if file is None:
            return _error(404, "notFound")
        if match.group(2):
            return self._export(file, params)
        if params.get("alt") == "media":
            return self._media(file)
        return httpx.Response(200, json=self._resource(file))

    @staticmethod
    def _resource(file: SimFile) -> dict[str, Any]:
        res: dict[str, Any] = {
            "id": file.id,
            "name": file.name,
            "mimeType": file.mime_type,
            "modifiedTime": file.modified_at.isoformat(),
            "owners": [
                {"displayName": o.name, "emailAddress": o.email}
                if o.name
                else {"emailAddress": o.email}
                for o in file.owners
            ],
            "trashed": file.trashed,
        }
        if file.mime_type not in GOOGLE_NATIVE and not file.mime_type.endswith(".folder"):
            res["size"] = str(
                file.size_bytes if file.size_bytes is not None else len(file.text.encode())
            )
        return res

    def _list(self, params: dict[str, str]) -> httpx.Response:
        try:
            predicate = _Parser(params.get("q", "trashed = false")).parse()
        except (BadQuery, ValueError):
            return _error(400, "invalid")
        if "fullText" in params.get("q", "") and "orderBy" in params:
            return _error(400, "badRequest")
        found = [f for f in self.files.values() if predicate(f)]
        if params.get("orderBy") == "modifiedTime desc":
            found.sort(key=lambda f: (-f.modified_at.timestamp(), f.id))
        size = int(params.get("pageSize", "100"))
        start = int(params["pageToken"].removeprefix("t:")) if "pageToken" in params else 0
        body: dict[str, Any] = {"files": [self._resource(f) for f in found[start : start + size]]}
        if start + size < len(found):
            body["nextPageToken"] = f"t:{start + size}"
        return httpx.Response(200, json=body)

    @staticmethod
    def _export(file: SimFile, params: dict[str, str]) -> httpx.Response:
        wanted = EXPORTS.get(file.mime_type)
        if wanted is None or params.get("mimeType") != wanted:
            return _error(403, "fileNotExportable")
        size = file.size_bytes if file.size_bytes is not None else len(file.text.encode())
        if size > LIMIT:
            return _error(403, "exportSizeLimitExceeded")
        return httpx.Response(200, content=file.text.encode(), headers={"Content-Type": wanted})

    @staticmethod
    def _media(file: SimFile) -> httpx.Response:
        if file.mime_type in GOOGLE_NATIVE:
            return _error(403, "fileNotDownloadable")
        return httpx.Response(
            200, content=file.text.encode(), headers={"Content-Type": file.mime_type}
        )


def make_transport(fake: FakeDrive) -> httpx.MockTransport:
    return httpx.MockTransport(fake)
