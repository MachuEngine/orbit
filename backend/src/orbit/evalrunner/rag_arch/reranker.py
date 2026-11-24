"""A1 reranker(ADR-0036). 로컬 cross-encoder. `sentence-transformers`는 첫 `score` 때만 import한다.

의존성은 평가 전용 그룹이다: `uv sync --group rag-eval`.
"""

import importlib
from collections.abc import Sequence
from typing import Any, Protocol

from orbit.evalrunner.rag_arch.config import (
    RERANKER_DEVICE,
    RERANKER_MAX_LENGTH,
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)


class Reranker(Protocol):
    model_id: str
    revision: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


class LocalCrossEncoderReranker:
    model_id = RERANKER_MODEL_ID
    revision = RERANKER_REVISION
    max_length = RERANKER_MAX_LENGTH
    device = RERANKER_DEVICE

    def __init__(self) -> None:
        self._model: Any = None

    def _load(self) -> Any:
        try:
            module = importlib.import_module("sentence_transformers")
        except ImportError as exc:
            raise RuntimeError(
                "A1은 sentence-transformers가 필요하다: uv sync --group rag-eval"
            ) from exc
        model = module.CrossEncoder(
            self.model_id, revision=self.revision, max_length=self.max_length, device=self.device
        )
        model.model.eval()
        return model

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if self._model is None:
            self._model = self._load()
        scores = self._model.predict([(query, p) for p in passages], show_progress_bar=False)
        return [float(x) for x in scores]
