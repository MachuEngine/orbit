"""커넥터 오류의 공용 분류 (ADR-0008, ADR-0016).

읽기 도구 결과(`{"error": 종류}`)와 화면 안내(`connector_notice` 이벤트)가 같은 종류 이름을 쓴다.
사유 코드(`reason`)는 어댑터가 이미 정제한 값만 비교한다.
"""

from orbit.connectors.contracts import (
    SCOPE_MISSING_REASON,
    ConnectorError,
    HistoryExpired,
    InvalidQuery,
    NotFound,
    PermissionRevoked,
    RateLimited,
    TransientError,
    UnsupportedFile,
)

_SERVICE_DISABLED = frozenset({"accessnotconfigured", "service_disabled"})
_QUOTA_EXCEEDED = frozenset({"quotaexceeded", "dailylimitexceeded"})

# 커넥터가 낸 오류라서 화면에 원인을 따로 알려야 하는 종류
CONNECTOR_NOTICE_KINDS = frozenset(
    {
        "service_disabled",
        "quota_exceeded",
        "rate_limited",
        "permission_revoked",
        "transient_error",
        "connector_error",
        "scope_missing",
    }
)


def connector_error_kind(exc: ConnectorError) -> str:
    """사유 코드를 먼저 보고, 없으면 오류 클래스로 종류를 정한다."""
    reason = exc.reason.casefold()
    if reason == SCOPE_MISSING_REASON:
        return "scope_missing"
    if reason in _SERVICE_DISABLED:
        return "service_disabled"
    if reason in _QUOTA_EXCEEDED:
        return "quota_exceeded"
    if isinstance(exc, HistoryExpired):
        return "history_expired"
    if isinstance(exc, UnsupportedFile):
        return "unsupported_file"
    if isinstance(exc, InvalidQuery):
        return "invalid_query"
    if isinstance(exc, NotFound):
        return "not_found"
    if isinstance(exc, RateLimited):
        return "rate_limited"
    if isinstance(exc, PermissionRevoked):
        return "permission_revoked"
    if isinstance(exc, TransientError):
        return "transient_error"
    return "connector_error"
