"""Connector 계약 v0 (ADR-0008): 오류, 값 객체, `CalendarConnector`·`MailConnector` Protocol.

- 계약 인스턴스는 연결 하나(계정 하나)에 묶인다. 워크스페이스·권한 판단은 계약 밖에서 한다.
- 모든 모델은 불변(pydantic v2 frozen)이며 시각은 시간대 정보가 있는 `datetime`만 받는다.
- 메서드는 모두 비동기다.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from orbit.core.ids import Uuid7Generator, uuid7

ORBIT_EXECUTION_HEADER = "X-Orbit-Execution-Id"

# ---------------------------------------------------------------- 오류


class ConnectorError(Exception):
    """계약이 던지는 모든 오류의 기반."""

    def __init__(self, message: str = "", *, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason  # 이미 정제된 안전한 사유 코드만 담는다(없으면 "")


class NotFound(ConnectorError):
    """대상이 없다."""


class AlreadyExists(ConnectorError):
    """클라이언트가 지정한 ID가 이미 있다."""


class PreconditionFailed(ConnectorError):
    """`if_match` 버전이 현재 버전과 다르다."""


class RateLimited(ConnectorError):
    """호출 한도 초과."""

    def __init__(
        self, *, retry_after_s: float | None = None, message: str = "", reason: str = ""
    ) -> None:
        super().__init__(message, reason=reason)
        self.retry_after_s = retry_after_s


class PermissionRevoked(ConnectorError):
    """연결 권한이 철회됐다."""


class InvalidQuery(ConnectorError):
    """지원하지 않는 검색 문법."""


class TransientError(ConnectorError):
    """일시 오류. 쓰기는 적용되지 않았음이 확실하다."""


class ResultUnknown(ConnectorError):
    """쓰기를 보냈지만 결과를 모른다. 재호출하지 말고 대조한다 (ADR-0003)."""


class HistoryExpired(ConnectorError):
    """변경 이력 위치가 만료됐다(ADR-0019 §2). 호출자는 전체 동기화로 돌아간다."""


class UnsupportedFile(ConnectorError):
    """읽을 수 없는 파일이다: 지원하지 않는 형식이거나 내보내기 한도(10MB)를 넘었다(ADR-0020 §2)."""


# 문서 권한(OAuth 범위)이 없을 때 `ConnectorError.reason`에 담는 사유 코드(ADR-0020 §2).
SCOPE_MISSING_REASON = "scope_missing"


# ---------------------------------------------------------------- 모델 공통


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- 캘린더 모델

ResponseStatus = Literal["needs_action", "accepted", "declined", "tentative"]
EventStatus = Literal["confirmed", "cancelled"]


class Calendar(_Model):
    id: str
    name: str
    time_zone: str = "UTC"
    primary: bool = False


class Attendee(_Model):
    email: str
    name: str | None = None
    response_status: ResponseStatus = "needs_action"


def _check_order(start: datetime | None, end: datetime | None) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError("end는 start보다 앞설 수 없다")


class CalendarEvent(_Model):
    """일정. 종일 일정(`all_day=True`)의 `start`·`end`는 UTC 00:00이고 `end`는 배타적이다.

    하루짜리 종일 일정은 `end = start + 1일`이다. v0 한계: 캘린더 `time_zone`은 종일 일정의
    날짜 경계에 쓰지 않고 항상 UTC 날짜 경계로 본다.
    """

    id: str
    calendar_id: str
    title: str
    description: str = ""
    location: str = ""
    start: AwareDatetime
    end: AwareDatetime
    all_day: bool = False
    attendees: tuple[Attendee, ...] = ()
    organizer: Attendee | None = None
    status: EventStatus = "confirmed"
    version: str
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _order(self) -> CalendarEvent:
        _check_order(self.start, self.end)
        return self


class EventDraft(_Model):
    """일정 생성 입력."""

    title: str
    start: AwareDatetime
    end: AwareDatetime
    description: str = ""
    location: str = ""
    all_day: bool = False
    attendees: tuple[Attendee, ...] = ()

    @model_validator(mode="after")
    def _order(self) -> EventDraft:
        _check_order(self.start, self.end)
        return self


class EventPatch(_Model):
    """일정 부분 수정 입력. `None`이 아닌 필드만 바꾼다(`attendees=()`는 참석자를 비운다).

    패치 자체는 `start`·`end`가 둘 다 있을 때만 순서를 검사한다. 현재 일정에 병합한 결과가
    `start < end`가 아니면(종일이면 날짜 기준) 구현체가 `ValueError`를 던지고 일정은 바뀌지 않는다.
    """

    title: str | None = None
    description: str | None = None
    location: str | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    all_day: bool | None = None
    attendees: tuple[Attendee, ...] | None = None

    @model_validator(mode="after")
    def _order(self) -> EventPatch:
        _check_order(self.start, self.end)
        return self

    def changes(self) -> dict[str, object]:
        """지정된(None이 아닌) 필드만 모은다."""
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if getattr(self, name) is not None
        }


# ---------------------------------------------------------------- 메일 모델


class Address(_Model):
    email: str
    name: str | None = None


class AttachmentMeta(_Model):
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)


class MailMessage(_Model):
    id: str
    thread_id: str
    from_: Address = Field(alias="from")
    to: tuple[Address, ...] = ()
    cc: tuple[Address, ...] = ()
    subject: str
    body_text: str
    snippet: str = ""
    sent_at: AwareDatetime
    labels: tuple[str, ...] = ()
    attachments: tuple[AttachmentMeta, ...] = ()
    headers: dict[str, str] = Field(default_factory=dict)

    @property
    def has_attachment(self) -> bool:
        return bool(self.attachments)


class MessageSummary(_Model):
    """검색 결과의 메시지 요약. 본문은 `get_message`로 읽는다."""

    id: str
    thread_id: str
    from_: Address = Field(alias="from")
    subject: str
    snippet: str = ""
    sent_at: AwareDatetime
    labels: tuple[str, ...] = ()
    has_attachment: bool = False


class MailThread(_Model):
    id: str
    messages: tuple[MailMessage, ...] = ()


class DraftSpec(_Model):
    to: tuple[Address, ...]
    cc: tuple[Address, ...] = ()
    subject: str
    body_text: str
    reply_to_message_id: str | None = None
    execution_id: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        """초안에 넣을 추가 헤더. `execution_id`가 있으면 `X-Orbit-Execution-Id`를 넣는다."""
        if self.execution_id is None:
            return {}
        return {ORBIT_EXECUTION_HEADER: self.execution_id}


class Draft(_Model):
    draft_id: str
    message_id: str
    thread_id: str
    spec: DraftSpec | None = None  # 조회·생성 응답에서 내용을 확인할 수 있게 선택으로 둔다


class SearchPage(_Model):
    messages: tuple[MessageSummary, ...] = ()
    next_page_token: str | None = None


class MailLabel(_Model):
    """메일 라벨. `kind`는 INBOX 같은 제공자 기본 라벨이면 `system`, 사용자 라벨이면 `user`."""

    id: str
    name: str
    kind: Literal["system", "user"]


class MessageIdPage(_Model):
    """라벨·기간 안의 메시지 ID 한 페이지(최근순). 본문은 `get_message`로 읽는다."""

    message_ids: tuple[str, ...] = ()
    next_page_token: str | None = None


class HistoryPage(_Model):
    """변경 이력 한 페이지(ADR-0019 §2).

    네 목록은 이력에 나온 메시지 ID이고 서로 겹칠 수 있다(한 메시지가 추가된 뒤 삭제될 수 있다).
    호출자는 삭제를 우선으로 보고, 나머지는 메시지를 다시 읽어 라벨 상태로 판단한다.
    `history_id`는 이 응답을 만든 시점의 이력 위치이며 다음 `list_history`의 시작점이다.
    """

    history_id: str
    added_ids: tuple[str, ...] = ()
    deleted_ids: tuple[str, ...] = ()
    labels_added_ids: tuple[str, ...] = ()
    labels_removed_ids: tuple[str, ...] = ()
    next_page_token: str | None = None


# ---------------------------------------------------------------- 문서(Drive) 모델 (ADR-0020 §2)

FileKind = Literal["document", "spreadsheet", "presentation", "pdf", "other"]
FILE_KINDS: tuple[FileKind, ...] = ("document", "spreadsheet", "presentation", "pdf", "other")

DOCS_MIME = "application/vnd.google-apps.document"
SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"
FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_MIME = "application/pdf"

# 종류 -> MIME 유형. `other`는 이 목록에 없는 유형 전부다(폴더는 검색 결과에서 늘 뺀다).
KIND_MIME_TYPES: dict[str, str] = {
    "document": DOCS_MIME,
    "spreadsheet": SHEETS_MIME,
    "presentation": SLIDES_MIME,
    "pdf": PDF_MIME,
}
# Google 문서류 -> 내보내기 MIME 유형. Sheets는 첫 시트만 나온다.
EXPORT_MIME_TYPES: dict[str, str] = {
    DOCS_MIME: "text/plain",
    SHEETS_MIME: "text/csv",
    SLIDES_MIME: "text/plain",
}
# 원문을 그대로 읽는 일반 텍스트 파일 유형
PLAIN_TEXT_MIME_TYPES: frozenset[str] = frozenset({"text/plain", "text/markdown", "text/csv"})
MAX_FILE_BYTES = 10 * 1024 * 1024  # 내보내기·내려받기 한도. 넘으면 `UnsupportedFile`
MAX_FILE_QUERY_CHARS = 200
MAX_FILE_PAGE_SIZE = 100


def normalize_mime(mime_type: str) -> str:
    """`text/plain; charset=utf-8` 같은 매개변수를 떼고 소문자로 맞춘다."""
    return mime_type.split(";", 1)[0].strip().lower()


def file_kind(mime_type: str) -> FileKind:
    mime = normalize_mime(mime_type)
    for kind, known in KIND_MIME_TYPES.items():
        if mime == known:
            return kind  # type: ignore[return-value]
    return "other"


def is_readable_mime(mime_type: str) -> bool:
    mime = normalize_mime(mime_type)
    return mime in EXPORT_MIME_TYPES or mime in PLAIN_TEXT_MIME_TYPES


class FileQuery(_Model):
    """파일 검색 조건(구조화). 모든 조건은 AND다. 휴지통과 폴더는 늘 뺀다.

    `text`는 이름 또는 본문에 들어 있는 글(대소문자 무시). `modified_after`는 이상,
    `modified_before`는 미만이다. 모델이 Drive 검색 문법을 직접 쓰지 않는다
    (어댑터가 이 조건으로 `q`를 만든다).
    """

    text: str | None = Field(default=None, max_length=MAX_FILE_QUERY_CHARS)
    kind: FileKind | None = None
    modified_after: AwareDatetime | None = None
    modified_before: AwareDatetime | None = None

    @model_validator(mode="after")
    def _order(self) -> FileQuery:
        _check_order(self.modified_after, self.modified_before)
        if self.text is not None and not self.text.strip():
            raise ValueError("text는 비어 있을 수 없다")
        return self


class DriveFile(_Model):
    """파일 메타데이터. 링크(`webViewLink` 등)는 계약에 넣지 않는다(D6-1)."""

    id: str
    name: str
    kind: FileKind
    mime_type: str
    modified_at: AwareDatetime
    owners: tuple[Address, ...] = ()
    size_bytes: int | None = Field(default=None, ge=0)


class FilePage(_Model):
    files: tuple[DriveFile, ...] = ()
    next_page_token: str | None = None


class FileText(_Model):
    """파일 본문 텍스트. `truncated`는 `max_chars`에서 잘렸다는 뜻이다.

    `file`은 읽은 파일의 메타데이터다(`get_file`을 따로 부르지 않아도 되게 함께 준다).
    """

    file: DriveFile
    text: str
    truncated: bool = False


# ---------------------------------------------------------------- Protocol


@runtime_checkable
class CalendarConnector(Protocol):
    """캘린더 계약 (ADR-0008 3절).

    계약 오류(`ConnectorError` 계열) 말고 다음 입력 위반은 호출자 버그이므로 `ValueError`다.
    장애 주입이나 재시도로 복구할 대상이 아니다.
    - `create_event`의 `event_id`가 Google 규칙(`[a-v0-9]` 5~1024자)에 맞지 않는다.
    - 종일 일정의 `start`·`end`가 UTC 00:00이 아니거나, 종일이 아닐 때 `start < end`가 아니다.
    - `update_event`의 패치를 병합한 결과가 `start < end`가 아니다(상태·버전은 바뀌지 않는다).
    - `list_events`의 `time_min`·`time_max`가 naive이거나 `time_min >= time_max`다.

    종일 일정은 UTC 00:00 기준, `end` 배타적이다(`CalendarEvent` 참고, v0 한계).
    """

    async def list_calendars(self) -> list[Calendar]: ...

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        query: str | None = None,
        include_cancelled: bool = False,
    ) -> list[CalendarEvent]: ...

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent: ...

    async def create_event(
        self, calendar_id: str, event_id: str, draft: EventDraft
    ) -> CalendarEvent: ...

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch, *, if_match: str
    ) -> CalendarEvent: ...

    async def cancel_event(
        self, calendar_id: str, event_id: str, *, if_match: str
    ) -> CalendarEvent: ...


@runtime_checkable
class MailConnector(Protocol):
    """메일 계약 (ADR-0008 4절).

    검색 문법은 부분집합(ADR-0008 4절, D1-6)이다: `from:`, `to:`, `subject:`, `after:`·`before:`
    (`YYYY/MM/DD`), `has:attachment`, `in:sent`·`in:inbox`·`in:drafts`, 일반 키워드, 큰따옴표 구문.
    조건은 모두 AND다. **모든 구현체는 질의를 외부로 보내기 전에 이 부분집합을 직접 검증하고,
    벗어나면 `InvalidQuery`를 던져야 한다.** 제공자가 우연히 받아 주는 `OR`·`-`·`label:` 같은
    연산자를 통과시키면 Sim과 실제 어댑터의 동작이 갈라진다.
    """

    async def search_messages(
        self, query: str, *, max_results: int = 20, page_token: str | None = None
    ) -> SearchPage: ...

    async def get_message(self, message_id: str) -> MailMessage: ...

    async def get_thread(self, thread_id: str) -> MailThread: ...

    async def create_draft(self, spec: DraftSpec) -> Draft: ...

    async def get_draft(self, draft_id: str) -> Draft: ...

    async def send_draft(self, draft_id: str) -> MailMessage: ...

    async def delete_draft(self, draft_id: str) -> None: ...

    # -- 색인용 읽기 (ADR-0019 §2). 새 OAuth 권한 없이 `gmail.readonly`로 가능하다.

    async def list_labels(self) -> list[MailLabel]: ...

    async def list_message_ids(
        self, label_id: str, *, after: datetime, page_token: str | None = None
    ) -> MessageIdPage:
        """`label_id`가 붙고 `after`(시간대 있는 시각) 이후인 메시지 ID를 최근순으로 돌려준다."""
        ...

    async def current_history_id(self) -> str: ...

    async def list_history(
        self, start_history_id: str, *, page_token: str | None = None
    ) -> HistoryPage:
        """`start_history_id` 이후의 변경. 위치가 만료됐으면 `HistoryExpired`."""
        ...


@runtime_checkable
class DriveConnector(Protocol):
    """문서(Drive) 읽기 계약 (ADR-0020 §2, ADR-0008 5절). 쓰기·링크·첨부 메서드는 없다.

    - 검색은 구조화된 `FileQuery`만 받는다. 모든 구현체는 휴지통과 폴더를 빼고, 텍스트 조건이
      없으면 최근 수정순(같으면 ID순)으로 돌려준다. 텍스트 조건이 있으면 순서는 구현체가 정한다
      (관련도순이며 Drive는 `fullText`와 정렬을 함께 쓸 수 없다). 호출자는 그 순서를 가정하지
      않는다. `max_results`는 1~100이 아니면 `ValueError`다.
    - `read_file_text`는 Google 문서는 `text/plain`, 스프레드시트는 첫 시트 `text/csv`,
      프레젠테이션은 `text/plain`으로 내보내고,
      일반 텍스트 파일(`text/plain`·`markdown`·`csv`)은 원문을 읽는다.
      그 밖의 형식과 10MB를 넘는 파일은 `UnsupportedFile`이다. `max_chars`는 1 이상이다.
    - 문서 권한(OAuth 범위)이 없으면 `ConnectorError`(사유 `scope_missing`)다.
    - 휴지통에 있거나 없는 파일은 `NotFound`다.
    """

    async def search_files(
        self, query: FileQuery, *, max_results: int = 20, page_token: str | None = None
    ) -> FilePage: ...

    async def get_file(self, file_id: str) -> DriveFile: ...

    async def read_file_text(self, file_id: str, *, max_chars: int) -> FileText: ...


# ---------------------------------------------------------------- 도구 이름


def _tool_names(protocol: type) -> frozenset[str]:
    """Protocol이 직접 정의한 공개 메서드 이름(`FaultPlan` 규칙의 `tool`로 쓴다)."""
    return frozenset(
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and callable(value)
    )


CALENDAR_TOOLS: frozenset[str] = _tool_names(CalendarConnector)
MAIL_TOOLS: frozenset[str] = _tool_names(MailConnector)
# 문서 읽기 메서드(ADR-0020). 장애 주입(FaultPlan)에는 쓰지만 평가 모델에는 노출하지 않고 평가
# 데이터의 금지 도구 목록(`CALENDAR_TOOLS | MAIL_TOOLS` 기준)에도 넣지 않는다.
DRIVE_TOOLS: frozenset[str] = _tool_names(DriveConnector)
# 색인 동기화 전용 읽기 메서드(ADR-0019 §2). 장애 주입(FaultPlan)에는 쓰지만 평가 모델에는 노출하지
# 않고 평가 데이터의 금지 도구 목록에도 넣지 않는다(9단계 이전과 같은 평가 도구 목록을 지킨다).
MAIL_SYNC_METHODS: frozenset[str] = frozenset(
    {"list_labels", "list_message_ids", "current_history_id", "list_history"}
)

# ---------------------------------------------------------------- 클라이언트 지정 일정 ID

_CLIENT_EVENT_ID = re.compile(r"[a-v0-9]{5,1024}")


def is_valid_client_event_id(value: str) -> bool:
    """Google 규칙: base32hex 소문자 `[a-v0-9]`, 5~1024자."""
    return _CLIENT_EVENT_ID.fullmatch(value) is not None


def validate_client_event_id(value: str) -> str:
    if not is_valid_client_event_id(value):
        raise ValueError("일정 ID는 [a-v0-9] 5~1024자여야 한다")
    return value


def event_id_from_uuid(value: uuid.UUID) -> str:
    """UUID를 하이픈 없는 16진 소문자 표기로 바꾼다(항상 유효한 일정 ID)."""
    return value.hex


def new_event_id(generator: Uuid7Generator | None = None) -> str:
    """UUIDv7(`orbit.core.ids`)로 클라이언트 지정 일정 ID를 만든다."""
    return event_id_from_uuid(uuid7(generator))
