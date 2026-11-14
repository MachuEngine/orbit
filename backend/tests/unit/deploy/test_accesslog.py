"""접근 로그에서 쿼리 문자열 제거 (ADR-0022 §5). 값은 모두 합성이다."""

import io
import logging
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from orbit.api.app import create_app
from orbit.core.accesslog import ACCESS_LOGGER, StripQueryFilter
from orbit.core.config import Settings

ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'  # uvicorn 접근 로그 형식과 같다
DB = SecretStr("postgresql+psycopg://x:y@localhost:1/z")


@pytest.fixture
def access_output() -> Iterator[io.StringIO]:
    logger = logging.getLogger(ACCESS_LOGGER)
    saved_filters, saved_level = list(logger.filters), logger.level
    logger.filters.clear()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield stream
    logger.removeHandler(handler)
    logger.filters[:] = saved_filters
    logger.setLevel(saved_level)


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_create_app_installs_filter_that_drops_query(env: str, access_output: io.StringIO) -> None:
    from google_mock import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SIGNING_KEY

    settings = Settings(
        database_url=DB,
        env=env,
        google_client_id=CLIENT_ID,
        google_client_secret=SecretStr(CLIENT_SECRET),
        google_redirect_uri=REDIRECT_URI,
        session_signing_key=SecretStr(SIGNING_KEY),
        login_allowed_emails="alice@example.test",
    )
    create_app(settings)
    create_app(settings)  # 두 번 설치해도 필터는 하나다
    logger = logging.getLogger(ACCESS_LOGGER)
    assert sum(isinstance(f, StripQueryFilter) for f in logger.filters) == 1

    logger.info(
        ACCESS_FORMAT,
        "127.0.0.1:5000",
        "GET",
        "/api/auth/google/callback?code=synthetic-code&state=synthetic-state",
        "1.1",
        302,
    )
    line = access_output.getvalue()
    assert "/api/auth/google/callback HTTP/1.1" in line
    assert line.rstrip().endswith("302")
    assert "synthetic-code" not in line and "synthetic-state" not in line and "?" not in line


def test_filter_leaves_other_records_untouched() -> None:
    f = StripQueryFilter()
    plain = logging.LogRecord(
        "x", logging.INFO, "p", 1, ACCESS_FORMAT, ("c", "GET", "/a", "1.1", 200), None
    )
    assert f.filter(plain) and plain.args == ("c", "GET", "/a", "1.1", 200)
    other = logging.LogRecord("x", logging.INFO, "p", 1, "no args", None, None)
    assert f.filter(other) and other.args is None
    short = logging.LogRecord("x", logging.INFO, "p", 1, "%s", ("q?x=1",), None)
    assert f.filter(short) and short.args == ("q?x=1",)
