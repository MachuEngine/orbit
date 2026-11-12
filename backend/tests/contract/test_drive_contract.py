"""DriveConnector 계약 테스트 (ADR-0008 5절, ADR-0020 §2). Sim과 모의 Drive 어댑터가 같이 통과한다.

구현체 무관: 계약 메서드만 쓴다. 검색어는 Drive 단어 단위 검색에서도 성립하게 완전한 낱말을 쓴다.
데이터는 `fixtures/sim/drive_world.json`(합성). 휴지통 파일 `f-trash-1`, 폴더 `f-folder-1`은
어느 검색에도 나오면 안 된다.
"""

from datetime import UTC, datetime

import pytest

from orbit.connectors.contracts import (
    ConnectorError,
    DriveConnector,
    FileQuery,
    NotFound,
    PermissionRevoked,
    UnsupportedFile,
)
from orbit.connectors.errors import connector_error_kind
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock

from .conftest import FIXTURE_PATH
from .drive_support import DRIVE_FACTORIES, FIXED_NOW, DriveFactory

WORLD_PATH = FIXTURE_PATH.with_name("drive_world.json")
ALICE, BOB = "acct-alice", "acct-bob"


def _world() -> SimWorld:
    return SimWorld.load(WORLD_PATH, FixedClock(FIXED_NOW))


@pytest.fixture(params=sorted(DRIVE_FACTORIES), ids=lambda n: n)
def factory(request: pytest.FixtureRequest) -> DriveFactory:
    return DRIVE_FACTORIES[request.param]


@pytest.fixture
def drive(factory: DriveFactory) -> DriveConnector:
    return factory(_world(), ALICE, False)


def _ids(page_files: tuple) -> list[str]:  # type: ignore[type-arg]
    return [f.id for f in page_files]


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


ALL_VISIBLE = [
    "f-doc-1",
    "f-sheet-1",
    "f-doc-2",
    "f-slides-1",
    "f-pdf-1",
    "f-txt-1",
    "f-big-1",
    "f-long-1",
]


async def test_search_all_is_recent_first_and_excludes_trash_and_folders(
    drive: DriveConnector,
) -> None:
    page = await drive.search_files(FileQuery())
    assert _ids(page.files) == ALL_VISIBLE
    assert page.next_page_token is None


async def test_text_matches_name_or_body(drive: DriveConnector) -> None:
    by_name = await drive.search_files(FileQuery(text="회의록"))
    assert _ids(by_name.files) == ["f-doc-1"]
    both = await drive.search_files(FileQuery(text="예산"))  # 문서 본문과 스프레드시트 이름
    assert sorted(_ids(both.files)) == ["f-doc-1", "f-sheet-1"]  # 텍스트가 있으면 순서는 가정 안 함
    assert (await drive.search_files(FileQuery(text="없는낱말"))).files == ()


async def test_text_with_quote_and_backslash_is_escaped(drive: DriveConnector) -> None:
    quote = await drive.search_files(FileQuery(text="O'Brien"))
    assert _ids(quote.files) == ["f-doc-2"]
    backslash = await drive.search_files(FileQuery(text="\\ 초안"))
    assert _ids(backslash.files) == ["f-doc-2"]
    # 이스케이프가 빠지면 조건을 빠져나가는 입력도 검색어 글자일 뿐이다.
    injected = await drive.search_files(FileQuery(text="x' or name contains '"))
    assert injected.files == ()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("document", ["f-doc-1", "f-doc-2", "f-big-1", "f-long-1"]),  # 텍스트 없음: 최근순
        ("spreadsheet", ["f-sheet-1"]),
        ("presentation", ["f-slides-1"]),
        ("pdf", ["f-pdf-1"]),
        ("other", ["f-txt-1"]),
    ],
)
async def test_kind_filter(drive: DriveConnector, kind: str, expected: list[str]) -> None:
    page = await drive.search_files(FileQuery(kind=kind))
    assert _ids(page.files) == expected


async def test_modified_after_is_inclusive_and_before_is_exclusive(drive: DriveConnector) -> None:
    after = await drive.search_files(FileQuery(modified_after=at(9, 9)))
    assert _ids(after.files) == ["f-doc-1", "f-sheet-1"]  # 11/9 09:00 정각은 포함
    before = await drive.search_files(FileQuery(modified_before=at(6, 9)))
    assert _ids(before.files) == ["f-txt-1", "f-big-1", "f-long-1"]  # 11/6 09:00 정각은 제외
    window = await drive.search_files(FileQuery(modified_after=at(7), modified_before=at(10)))
    assert _ids(window.files) == ["f-sheet-1", "f-doc-2", "f-slides-1"]


async def test_conditions_are_anded(drive: DriveConnector) -> None:
    page = await drive.search_files(
        FileQuery(text="예산", kind="spreadsheet", modified_after=at(9))
    )
    assert _ids(page.files) == ["f-sheet-1"]
    none = await drive.search_files(FileQuery(text="예산", kind="presentation"))
    assert none.files == ()


async def test_paging_walks_every_result_once(drive: DriveConnector) -> None:
    seen: list[str] = []
    token: str | None = None
    for _ in range(10):
        page = await drive.search_files(FileQuery(), max_results=3, page_token=token)
        assert len(page.files) <= 3
        seen += _ids(page.files)
        token = page.next_page_token
        if token is None:
            break
    assert seen == ALL_VISIBLE


@pytest.mark.parametrize("bad", [0, 101])
async def test_max_results_out_of_range_is_a_value_error(drive: DriveConnector, bad: int) -> None:
    with pytest.raises(ValueError, match="max_results"):
        await drive.search_files(FileQuery(), max_results=bad)


async def test_get_file_returns_metadata(drive: DriveConnector) -> None:
    file = await drive.get_file("f-doc-1")
    assert (file.name, file.kind, file.modified_at) == ("분기 회의록", "document", at(10, 9))
    assert [(o.email, o.name) for o in file.owners] == [("alice@example.com", "앨리스")]
    pdf = await drive.get_file("f-pdf-1")
    assert (pdf.kind, pdf.mime_type, pdf.size_bytes) == ("pdf", "application/pdf", 2048)
    assert (await drive.get_file("f-txt-1")).kind == "other"


@pytest.mark.parametrize("file_id", ["f-nope", "f-trash-1"])
async def test_missing_or_trashed_file_is_not_found(drive: DriveConnector, file_id: str) -> None:
    with pytest.raises(NotFound):
        await drive.get_file(file_id)
    with pytest.raises(NotFound):
        await drive.read_file_text(file_id, max_chars=100)


@pytest.mark.parametrize(
    ("file_id", "expected"),
    [
        ("f-doc-1", "분기 회의록 본문입니다. 예산 논의와 일정 합의가 있었습니다."),
        ("f-sheet-1", "항목,금액\n장비,100\n"),  # 스프레드시트는 CSV
        ("f-slides-1", "슬라이드 하나\n슬라이드 둘"),
        ("f-txt-1", "plain memo content"),  # 일반 텍스트 파일은 원문
    ],
)
async def test_read_supported_kinds(drive: DriveConnector, file_id: str, expected: str) -> None:
    result = await drive.read_file_text(file_id, max_chars=1000)
    assert (result.text, result.truncated) == (expected, False)


async def test_read_truncates_at_max_chars(drive: DriveConnector) -> None:
    cut = await drive.read_file_text("f-long-1", max_chars=10)
    assert (cut.text, cut.truncated) == ("0123456789", True)
    exact = await drive.read_file_text("f-long-1", max_chars=40)
    assert (len(exact.text), exact.truncated) == (40, False)
    with pytest.raises(ValueError, match="max_chars"):
        await drive.read_file_text("f-long-1", max_chars=0)


@pytest.mark.parametrize("file_id", ["f-pdf-1", "f-big-1", "f-folder-1"])
async def test_unreadable_files_are_unsupported(drive: DriveConnector, file_id: str) -> None:
    with pytest.raises(UnsupportedFile) as caught:
        await drive.read_file_text(file_id, max_chars=100)
    assert connector_error_kind(caught.value) == "unsupported_file"


async def test_accounts_are_isolated(factory: DriveFactory) -> None:
    world = _world()
    bob = factory(world, BOB, False)
    assert _ids((await bob.search_files(FileQuery())).files) == ["f-bob-1"]
    with pytest.raises(NotFound):
        await bob.get_file("f-doc-1")


async def test_missing_scope_is_scope_missing_not_revoked(factory: DriveFactory) -> None:
    denied = factory(_world(), ALICE, True)
    calls = (
        lambda: denied.search_files(FileQuery()),
        lambda: denied.get_file("f-doc-1"),
        lambda: denied.read_file_text("f-doc-1", max_chars=10),
    )
    for call in calls:
        with pytest.raises(ConnectorError) as caught:
            await call()
        assert not isinstance(caught.value, PermissionRevoked)
        assert caught.value.reason == "scope_missing"
        assert connector_error_kind(caught.value) == "scope_missing"
