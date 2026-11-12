"""커넥터 오류 사유 코드 전달과 공용 분류 (7단계 이슈 1). 데이터는 합성 값이다."""

from collections.abc import Mapping

import httpx
import pytest

from orbit.connectors.contracts import (
    ConnectorError,
    InvalidQuery,
    NotFound,
    PermissionRevoked,
    RateLimited,
    TransientError,
)
from orbit.connectors.errors import connector_error_kind
from orbit.connectors.google._http import http_error


def _resp(status: int, body: Mapping[str, object] | None = None) -> httpx.Response:
    return httpx.Response(status, json=dict(body or {}))


def _google(status: int, reason: str, *, query: bool = False) -> ConnectorError:
    body = {"error": {"code": status, "message": "합성 메시지", "errors": [{"reason": reason}]}}
    return http_error(_resp(status, body), service="calendar", op="list", write=False, query=query)


def test_default_reason_is_empty_and_existing_calls_still_work() -> None:
    assert ConnectorError("x").reason == ""
    assert RateLimited(retry_after_s=1.0, message="x").reason == ""
    assert RateLimited(reason="rateLimitExceeded").reason == "rateLimitExceeded"


@pytest.mark.parametrize(
    ("status", "reason", "kind"),
    [
        (403, "accessNotConfigured", "service_disabled"),
        (403, "ACCESSNOTCONFIGURED", "service_disabled"),
        (403, "SERVICE_DISABLED", "service_disabled"),
        (403, "quotaExceeded", "quota_exceeded"),
        (403, "dailyLimitExceeded", "quota_exceeded"),
        (403, "rateLimitExceeded", "rate_limited"),
        (429, "", "rate_limited"),
        (401, "", "permission_revoked"),
        (403, "insufficientPermissions", "permission_revoked"),
        (404, "notFound", "not_found"),
        (500, "backendError", "transient_error"),
        (403, "forbidden", "connector_error"),
    ],
)
def test_http_error_carries_reason_and_classifies(status: int, reason: str, kind: str) -> None:
    err = _google(status, reason)
    assert connector_error_kind(err) == kind
    assert err.reason == (reason if reason else "")


def test_invalid_query_class_and_unsafe_reason() -> None:
    assert connector_error_kind(_google(400, "invalid", query=True)) == "invalid_query"
    unsafe = _google(403, "has space and 문장")
    assert unsafe.reason == "unknown"
    assert connector_error_kind(unsafe) == "connector_error"


def test_class_based_kinds_without_reason() -> None:
    assert connector_error_kind(InvalidQuery("x")) == "invalid_query"
    assert connector_error_kind(NotFound("x")) == "not_found"
    assert connector_error_kind(PermissionRevoked("x")) == "permission_revoked"
    assert connector_error_kind(TransientError("x")) == "transient_error"
    assert connector_error_kind(ConnectorError("x")) == "connector_error"
