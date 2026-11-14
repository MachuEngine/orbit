"""uvicorn 접근 로그에서 쿼리 문자열을 지운다 (ADR-0022 §5).

OAuth 콜백 주소(`?code=...&state=...`)와 SSE·검색 요청의 쿼리가 로그 저장소에 남지 않게 한다.
uvicorn은 접근 로그를 `uvicorn.access` 로거에 `args = (클라이언트, 메서드, 경로+쿼리, HTTP 버전,
상태)`로 남기므로, 세 번째 인자에서 `?` 뒤를 자른다. dev와 prod 모두 `create_app`이 설치한다.
"""

import logging

ACCESS_LOGGER = "uvicorn.access"
_PATH_ARG = 2


class StripQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) <= _PATH_ARG:
            return True
        path = args[_PATH_ARG]
        if isinstance(path, str) and "?" in path:
            record.args = (*args[:_PATH_ARG], path.split("?", 1)[0], *args[_PATH_ARG + 1 :])
        return True


def install_access_log_filter() -> None:
    """여러 번 불러도 필터는 하나만 붙는다."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, StripQueryFilter) for f in logger.filters):
        logger.addFilter(StripQueryFilter())
