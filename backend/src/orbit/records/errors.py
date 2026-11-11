"""기록 계층 예외. 메시지에는 ID와 상태 이름만 넣는다(본문·주소·이름 금지, ADR-0006)."""


class RecordsError(Exception):
    """기록 계층 공통 예외."""


class RecordNotFound(RecordsError):
    """워크스페이스 안에서 대상 행을 찾지 못했다."""


class InvalidTransition(RecordsError):
    """허용되지 않은 상태 전이다."""


class IdempotencyConflict(RecordsError):
    """같은 멱등 키로 다른 내용을 기록하려 했다."""


class ApprovalNotApproved(RecordsError):
    """승인되지 않은 approval에는 실행 기록을 만들 수 없다."""


class InvalidJobArgs(RecordsError):
    """작업 인자가 `run_id`·`workspace_id` UUID 문자열 두 개가 아니다."""


class RunHasActiveExecution(RecordsError):
    """`pending`·`unknown` 실행이 남은 run은 지울 수 없다(결과가 확정되지 않은 외부 쓰기)."""
