"""시스템 브라우저에 보여 주는 데스크톱 연결 시작 안내 화면 (ADR-0024 §5).

사용자 입력을 문구에 넣지 않는다. 티켓은 형식 검증(`[A-Za-z0-9_-]`)을 마친 값만 숨은 입력에 넣는다.
스크립트·외부 리소스 없이 인라인 스타일만 쓰고 응답 헤더로 `default-src 'none'`을 건다.
"""

from html import escape

from fastapi import Response

_STYLE = (
    "body{font-family:-apple-system,system-ui,sans-serif;max-width:28rem;margin:20vh auto;"
    "padding:0 1.5rem;line-height:1.6;color:#222}h1{font-size:1.25rem}"
    "button{font:inherit;padding:.5rem 1.25rem;cursor:pointer}"
)
_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _document(body: str, status_code: int) -> Response:
    html = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Orbit</title><style>{_STYLE}</style></head><body><main>{body}</main></body></html>"
    )
    return Response(html, status_code=status_code, media_type="text/html", headers=_HEADERS)


def failed_page() -> Response:
    return _document(
        "<h1>연결하지 못했습니다</h1>"
        "<p>Orbit 앱으로 돌아가 다시 시도해 주세요. 계속 실패하면 서버 설정을 확인해 주세요.</p>",
        400,
    )


def begin_page(*, action: str, ticket: str, token: str) -> Response:
    """ "계속"을 눌러야 연결이 시작되는 안내 화면. GET(미리보기·프리페치)은 티켓을 소비하지
    않는다."""
    return _document(
        "<h1>Google 계정 연결</h1>"
        "<p>Orbit 앱에서 시작한 Google 계정 연결입니다. 방금 Orbit 앱에서 연결을 눌렀다면 "
        "계속을 눌러 주세요. 직접 시작한 것이 아니면 이 창을 닫아 주세요.</p>"
        f'<form method="post" action="{escape(action)}">'
        f'<input type="hidden" name="ticket" value="{escape(ticket)}">'
        f'<input type="hidden" name="token" value="{escape(token)}">'
        '<button type="submit">계속</button></form>',
        200,
    )
