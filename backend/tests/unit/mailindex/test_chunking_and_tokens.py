"""청크 분할과 형태소 토큰화 (ADR-0019 §4). 데이터는 모두 합성이다."""

from orbit.mailindex.chunking import index_text, split_chunks
from orbit.mailindex.limits import (
    CHUNK_CHARS,
    CHUNK_OVERLAP_CHARS,
    MAX_BODY_CHARS,
    MAX_CHUNKS_PER_MAIL,
)
from orbit.mailindex.search import to_tsquery_text
from orbit.mailindex.tokenize import tokenize


def test_short_mail_is_one_chunk_with_subject_first() -> None:
    chunks = split_chunks("견적 문의", "단가와 납기를 알려 주세요.")
    assert chunks == ["견적 문의\n단가와 납기를 알려 주세요."]


def test_chunks_are_about_800_chars_and_overlap_100() -> None:
    body = " ".join(f"단어{i:04d}" for i in range(400))  # 약 3,200자
    chunks = split_chunks("제목", body)
    assert len(chunks) >= 4
    assert all(0 < len(c) <= CHUNK_CHARS for c in chunks)
    for left, right in zip(chunks, chunks[1:], strict=False):
        tail = left[-CHUNK_OVERLAP_CHARS:]
        assert any(word in right[: CHUNK_OVERLAP_CHARS + 20] for word in tail.split()[1:])
    # 모든 단어가 어느 청크에든 들어 있다(빠지는 구간이 없다).
    joined = "\n".join(chunks)
    assert all(f"단어{i:04d}" in joined for i in range(400))


def test_chunk_count_and_body_length_are_capped() -> None:
    huge = "가" * (MAX_BODY_CHARS * 3)
    assert len(split_chunks("제목", huge)) == MAX_CHUNKS_PER_MAIL
    # 본문은 앞 20,000자까지만 본다.
    assert len(index_text("제목", huge)) <= len("제목\n") + MAX_BODY_CHARS


def test_empty_mail_has_no_chunks() -> None:
    assert split_chunks("", "   \n ") == []


def test_splitting_is_deterministic() -> None:
    body = "합성 문장입니다. " * 300
    assert split_chunks("제목", body) == split_chunks("제목", body)


def test_tokenize_keeps_content_words_only() -> None:
    tokens = tokenize("지난달 견적 얘기한 메일을 찾아 주세요")
    assert {"지난달", "견적", "얘기", "메일"} <= set(tokens)
    assert "을" not in tokens and "주세요" not in tokens  # 조사·어미는 뺀다


def test_tokenize_lowercases_and_keeps_codes_and_english() -> None:
    tokens = tokenize("Invoice PO-2026-117 납기 확인")
    assert "invoice" in tokens and "납기" in tokens
    assert all(t == t.lower() and t.isalnum() for t in tokens)
    assert any("2026" in t for t in tokens)


def test_tokenize_empty_and_punctuation_only() -> None:
    assert tokenize("") == []
    assert tokenize("  ?!  ") == []


def test_query_and_document_use_the_same_tokens() -> None:
    assert tokenize("납기 일정") == tokenize("납기 일정")
    assert set(tokenize("납기를 확인해 주세요")) & set(tokenize("납기 확인")) == {"납기", "확인"}


def test_tsquery_text_is_quoted_or_query_without_operators() -> None:
    q = to_tsquery_text(["견적", "납기", "견적", "a&b|c", "!!", "x'y"])
    assert q == "'견적' | '납기' | 'abc' | 'xy'"
    assert to_tsquery_text(["!!", ""]) == ""
