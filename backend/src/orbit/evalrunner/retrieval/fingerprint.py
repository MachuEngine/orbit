"""색인 지문 (ADR-0028 §2). 같은 지문이면 같은 색인이므로 재사용하고, 다르면 새로 만든다."""

import hashlib
import json
import uuid
from typing import Any

from orbit.evalrunner.retrieval import worldmail
from orbit.mailindex.limits import (
    CHUNK_CHARS,
    CHUNK_OVERLAP_CHARS,
    EMBEDDING_DIM,
    INDEX_CODE_VERSION,
    MAX_BODY_CHARS,
    MAX_CHUNKS_PER_MAIL,
)
from orbit.mailindex.tokenize import tokenizer_config

_NAMESPACE = uuid.UUID("5d1b7f0e-3c2a-4f6e-9a55-0e11a7d0a001")


def index_fingerprint_payload(
    *, world_sha256: str, account_id: str, model_id: str, first_sync_max: int
) -> dict[str, Any]:
    return {
        "world_sha256": world_sha256,
        "account_id": account_id,
        "chunk": {
            "chars": CHUNK_CHARS,
            "overlap_chars": CHUNK_OVERLAP_CHARS,
            "max_body_chars": MAX_BODY_CHARS,
            "max_chunks_per_mail": MAX_CHUNKS_PER_MAIL,
        },
        "tokenizer": tokenizer_config(),
        "embedding": {"model_id": model_id, "dim": EMBEDDING_DIM},
        "index_code_version": INDEX_CODE_VERSION,
        "scope": {
            "labels": list(worldmail.SCOPE_LABELS),
            "period_days": worldmail.SCOPE_PERIOD_DAYS,
            "index_now": worldmail.INDEX_NOW.isoformat(),
            "first_sync_max": first_sync_max,
        },
    }


def digest(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def index_fingerprint(
    *, world_sha256: str, account_id: str, model_id: str, first_sync_max: int
) -> str:
    return digest(
        index_fingerprint_payload(
            world_sha256=world_sha256,
            account_id=account_id,
            model_id=model_id,
            first_sync_max=first_sync_max,
        )
    )


def derived_id(fingerprint: str, role: str) -> uuid.UUID:
    """지문과 역할(workspace·user·connection)에서 정한 ID. 같은 지문은 같은 워크스페이스다."""
    return uuid.uuid5(_NAMESPACE, f"{fingerprint}:{role}")
