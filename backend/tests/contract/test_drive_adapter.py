"""Google Drive Adapter 테스트(모의 서버) (ADR-0020 §2). 데이터는 합성 값이다."""

from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from orbit.connectors.contracts import (
    ConnectorError,
    FileQuery,
    NotFound,
    PermissionRevoked,
    RateLimited,
    UnsupportedFile,
)
from orbit.connectors.errors import connector_error_kind
from orbit.connectors.google._http import http_error
from orbit.connectors.google.drive import GoogleDriveConnector, escape_q, to_drive_query
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock

from .drive_support import build_google_mock
from .fake_drive import FakeDrive

WORLD = {
    "schema_version": 1,
    "seed": 1,
    "accounts": {
        "a": {
            "email": "a@example.com",
            "files": [
                {
                    "id": "d1",
                    "name": "문서",
                    "mime_type": "application/vnd.google-apps.document",
                    "modified_at": "2026-11-10T09:00:00+00:00",
                    "text": "본문",
                },
                {
                    "id": "t1",
                    "name": "텍스트.txt",
                    "mime_type": "text/plain",
                    "modified_at": "2026-11-09T09:00:00+00:00",
                    "text": "원문",
                    "size_bytes": 11 * 1024 * 1024,
                },
            ],
        }
    },
}


def _connector() -> tuple[GoogleDriveConnector, FakeDrive]:
    world = SimWorld.load(WORLD, FixedClock(datetime(2026, 11, 12, tzinfo=UTC)))
    return build_google_mock(world, "a")


def test_escape_handles_backslash_before_quote() -> None:
    assert escape_q("a'b") == "a\\'b"
    assert escape_q("a\\b") == "a\\\\b"
    assert escape_q("\\'") == "\\\\\\'"  # 역슬래시를 먼저 이스케이프한다


def test_query_without_conditions_excludes_trash_and_folders_only() -> None:
    assert to_drive_query(FileQuery()) == (
        "trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    )


def test_query_combines_every_condition_with_and() -> None:
    kst = timezone(timedelta(hours=9))
    q = to_drive_query(
        FileQuery(
            text=" 회의록 ",
            kind="document",
            modified_after=datetime(2026, 11, 5, 9, 0, tzinfo=kst),
            modified_before=datetime(2026, 11, 10, 0, 0, 0, 500000, tzinfo=UTC),
        )
    )
    assert q == (
        "trashed = false and mimeType != 'application/vnd.google-apps.folder'"
        " and (name contains '회의록' or fullText contains '회의록')"
        " and mimeType = 'application/vnd.google-apps.document'"
        " and modifiedTime >= '2026-11-05T00:00:00.000Z'"
        " and modifiedTime < '2026-11-10T00:00:00.500Z'"
    )


def test_query_other_kind_excludes_the_named_kinds() -> None:
    q = to_drive_query(FileQuery(kind="other"))
    for mime in ("document", "spreadsheet", "presentation"):
        assert f"mimeType != 'application/vnd.google-apps.{mime}'" in q
    assert "mimeType != 'application/pdf'" in q


def test_query_escapes_quote_and_backslash_in_text() -> None:
    q = to_drive_query(FileQuery(text="a'b\\c"))
    assert "name contains 'a\\'b\\\\c'" in q and "fullText contains 'a\\'b\\\\c'" in q


def test_query_model_rejects_blank_text_naive_time_and_reversed_range() -> None:
    with pytest.raises(ValueError, match="text"):
        FileQuery(text="  ")
    with pytest.raises(ValueError, match="end"):
        FileQuery(
            modified_after=datetime(2026, 11, 2, tzinfo=UTC),
            modified_before=datetime(2026, 11, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="tz|time|aware"):
        FileQuery(modified_after=datetime(2026, 11, 2))  # noqa: DTZ001


async def test_requests_ask_for_minimal_fields_without_links() -> None:
    drive, fake = _connector()
    await drive.search_files(FileQuery(text="문서"))
    await drive.get_file("d1")
    await drive.read_file_text("d1", max_chars=10)
    for _, _, params in fake.calls:
        fields = params.get("fields", "")
        assert "webViewLink" not in fields and "webContentLink" not in fields
        assert "thumbnailLink" not in fields and "iconLink" not in fields
    listing = fake.calls[0][2]
    assert (
        "orderBy" not in listing and listing["spaces"] == "drive"
    )  # fullText와 정렬은 함께 못 쓴다
    await drive.search_files(FileQuery(kind="document"))
    assert fake.calls[-1][2]["orderBy"] == "modifiedTime desc"  # 텍스트가 없을 때만 최근순
    assert "files(" in listing["fields"] and "owners(displayName,emailAddress)" in listing["fields"]


async def test_export_uses_the_kind_specific_mime_and_media_for_text() -> None:
    drive, fake = _connector()
    await drive.read_file_text("d1", max_chars=10)
    assert any(
        path.endswith("/files/d1/export") and params == {"mimeType": "text/plain"}
        for _, path, params in fake.calls
    )


async def test_text_file_over_size_limit_is_unsupported_before_download() -> None:
    drive, fake = _connector()
    with pytest.raises(UnsupportedFile):
        await drive.read_file_text("t1", max_chars=10)
    assert not any(params.get("alt") == "media" for _, _, params in fake.calls)


async def test_file_id_is_percent_encoded_in_the_path() -> None:
    drive, fake = _connector()
    with pytest.raises(NotFound):
        await drive.get_file("../files?x=1")
    assert fake.raw_paths[-1] == b"/drive/v3/files/..%2Ffiles%3Fx%3D1"


async def test_error_messages_carry_no_names_or_bodies() -> None:
    drive, fake = _connector()
    fake.scope_missing = True
    with pytest.raises(ConnectorError) as caught:
        await drive.get_file("d1")
    text = str(caught.value)
    assert "문서" not in text and "drive-mock-token" not in text


def _google(status: int, reason: str, detail: str | None = None) -> ConnectorError:
    err: dict[str, object] = {"code": status, "errors": [{"reason": reason}]}
    if detail:
        err["details"] = [{"reason": detail}]
    resp = httpx.Response(status, json={"error": err})
    return http_error(resp, service="Drive", op="x", write=False, drive=True)


@pytest.mark.parametrize(
    ("status", "reason", "detail", "kind"),
    [
        (403, "insufficientPermissions", None, "scope_missing"),
        (403, "forbidden", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "scope_missing"),
        (403, "exportSizeLimitExceeded", None, "unsupported_file"),
        (403, "fileNotDownloadable", None, "unsupported_file"),
        (403, "rateLimitExceeded", None, "rate_limited"),
        (403, "accessNotConfigured", None, "service_disabled"),
        (401, "", None, "permission_revoked"),
        (404, "notFound", None, "not_found"),
        (500, "backendError", None, "transient_error"),
    ],
)
def test_drive_error_classification(
    status: int, reason: str, detail: str | None, kind: str
) -> None:
    assert connector_error_kind(_google(status, reason, detail)) == kind


def test_other_services_still_treat_insufficient_permissions_as_revoked() -> None:
    resp = httpx.Response(
        403, json={"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}}
    )
    err = http_error(resp, service="Gmail", op="x", write=False)
    assert isinstance(err, PermissionRevoked)
    assert not isinstance(_google(429, ""), PermissionRevoked)
    assert isinstance(_google(429, ""), RateLimited)


async def test_fake_drive_rejects_fulltext_with_order_by() -> None:
    _, fake = _connector()
    resp = fake(
        httpx.Request(
            "GET",
            "https://drive.mock/drive/v3/files",
            params={"q": "fullText contains 'a'", "orderBy": "modifiedTime desc"},
            headers={"Authorization": f"Bearer {fake.valid_token}"},
        )
    )
    assert resp.status_code == 400


async def test_reading_a_file_costs_one_metadata_call_and_one_content_call() -> None:
    drive, fake = _connector()
    result = await drive.read_file_text("d1", max_chars=10)
    assert result.file.name == "문서" and result.file.kind == "document"
    assert len(fake.calls) == 2  # files.get 한 번 + files.export 한 번
