"""메일 본문 청크 분할 (ADR-0019 §4). 제목과 본문을 이어 약 800자 단위, 100자 겹침으로 자른다."""

from orbit.mailindex.limits import (
    CHUNK_CHARS,
    CHUNK_OVERLAP_CHARS,
    MAX_BODY_CHARS,
    MAX_CHUNKS_PER_MAIL,
)

# 창 끝에서 이 범위 안에 공백·줄바꿈이 있으면 거기서 자른다(단어 중간을 피한다).
_BREAK_WINDOW = CHUNK_OVERLAP_CHARS


def index_text(subject: str, body: str) -> str:
    """색인 대상 텍스트. 본문은 앞 `MAX_BODY_CHARS`자만 쓴다."""
    clipped = body[:MAX_BODY_CHARS].replace("\r\n", "\n").replace("\r", "\n")
    return f"{subject.strip()}\n{clipped}".strip()


def split_chunks(subject: str, body: str) -> list[str]:
    """제목+본문을 청크 목록으로 나눈다. 최대 `MAX_CHUNKS_PER_MAIL`개이고 빈 청크는 없다."""
    text = index_text(subject, body)
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < MAX_CHUNKS_PER_MAIL:
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            cut = max(
                text.rfind("\n", end - _BREAK_WINDOW, end),
                text.rfind(" ", end - _BREAK_WINDOW, end),
            )
            if cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP_CHARS
    return chunks
