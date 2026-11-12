"""색인·검색 상수 (ADR-0019 §1·§4·§5). `실험 중` 값이며 한곳에서만 정한다.

값을 바꾸면 `INDEX_CONFIG`가 바뀌어 `run.config_fingerprint`도 달라진다(재현성, D9-3).
"""

# 청크 (ADR-0019 §4): 제목과 본문을 이어 자른다.
CHUNK_CHARS = 800
CHUNK_OVERLAP_CHARS = 100

# 비용 상한 (ADR-0019 §1)
MAX_BODY_CHARS = 20_000  # 메일 하나에서 색인하는 본문 앞부분
MAX_CHUNKS_PER_MAIL = 20
FIRST_SYNC_MAX_MAILS = 2_000  # 범위 하나의 첫 동기화에서 읽는 최근 메일 수
PERIOD_DAYS = (30, 90, 365)  # 사용자가 고르는 범위 기간

# 임베딩
EMBEDDING_DIM = 1536  # chunk.embedding 열 차원(text-embedding-3-small)
EMBED_BATCH_SIZE = 64

# 검색 (ADR-0019 §5)
VECTOR_CANDIDATES = 50
KEYWORD_CANDIDATES = 50
RRF_K = 60
MAX_QUERY_TOKENS = 32
HNSW_EF_SEARCH = 100

# 비서 도구 결과
DEFAULT_RESULTS = 5
MAX_RESULTS = 20
MAX_QUERY_CHARS = 500

# 설정 지문에 넣는 상수 묶음
INDEX_CONFIG: dict[str, int] = {
    "chunk_chars": CHUNK_CHARS,
    "chunk_overlap_chars": CHUNK_OVERLAP_CHARS,
    "max_body_chars": MAX_BODY_CHARS,
    "max_chunks_per_mail": MAX_CHUNKS_PER_MAIL,
    "first_sync_max_mails": FIRST_SYNC_MAX_MAILS,
    "embedding_dim": EMBEDDING_DIM,
    "vector_candidates": VECTOR_CANDIDATES,
    "keyword_candidates": KEYWORD_CANDIDATES,
    "rrf_k": RRF_K,
}

DEFAULT_EMBEDDING_MODEL_ID = "text-embedding-3-small"
