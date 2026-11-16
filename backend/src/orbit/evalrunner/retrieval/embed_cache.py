"""임베딩 캐시 (ADR-0028 §2). `(임베딩 모델 ID, 텍스트 sha256) -> 벡터`를 JSONL에 덧붙여 쌓는다.

- 파일은 모델마다 하나(`<캐시 폴더>/<모델 ID>.jsonl`)이고 git에서 제외한 `runs/` 아래에 둔다.
- 벡터는 float32 little-endian을 base64로 저장한다. pgvector도 float32로 저장하므로 색인은 같다.
- 래퍼는 `Embedder`를 감싸 적중·미적중 수를 센다. 미적중 텍스트는 한 번에 안쪽 임베더로 보낸다.
- 누적 비용 상한을 넘을 것 같으면 호출 전에 `BudgetExceeded`로 멈춘다(추정 토큰 기준, 호출 뒤에는
  응답의 실제 토큰 수가 있으면 그 값으로 센다).
- 텍스트 원문은 파일에 저장하지 않는다(해시와 벡터만).
"""

import base64
import hashlib
import json
import math
import re
import struct
import sys
from collections.abc import Sequence
from pathlib import Path

from orbit.mailindex.embedding import Embedder
from orbit.mailindex.limits import EMBED_BATCH_SIZE, EMBEDDING_DIM

# 토큰 추정: 한국어 메일은 글자당 1토큰 안팎이라 보수적으로 글자당 1토큰으로 센다. 측정값이
# 아니다. 실제 호출 뒤에는 응답의 usage를 쓰고, 추정과 실제의 차이를 결과에 남긴다.
EST_TOKENS_PER_CHAR = 1.0
# 1백만 토큰당 달러(입력). 출처: OpenAI 가격표 확인값을 개발자가 정한다. 모르는 모델은 None.
PRICE_PER_MTOK_USD: dict[str, float] = {"text-embedding-3-small": 0.02, "fake-embedding-v1": 0.0}

_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


class CacheCorruptError(ValueError):
    """캐시 파일의 중간 줄이 손상됐다. 마지막 줄이 아니므로 자동으로 복구하지 않는다."""


class BudgetExceeded(RuntimeError):  # noqa: N818  도메인 용어
    """누적 임베딩 비용 상한을 넘을 것 같아 호출하지 않았다."""


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) * EST_TOKENS_PER_CHAR)


def estimate_cost_usd(
    model_id: str, tokens: int, price_per_mtok: float | None = None
) -> float | None:
    price = price_per_mtok if price_per_mtok is not None else PRICE_PER_MTOK_USD.get(model_id)
    return None if price is None else round(tokens * price / 1_000_000, 8)


def _pack(vector: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def _unpack(blob: str) -> list[float]:
    raw = base64.b64decode(blob)
    return [float(x) for x in struct.unpack(f"<{len(raw) // 4}f", raw)]


def _load_rows(path: Path) -> tuple[list[dict[str, str]], bool]:
    """(행 목록, 마지막 줄을 버렸는지). 마지막 줄만 깨졌으면(쓰다 끊긴 경우) 그 줄을 버리고
    경고를 한 줄 낸다. 중간 줄이 깨졌으면 `CacheCorruptError`다."""
    lines = [(n, ln) for n, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)]
    lines = [(n, ln) for n, ln in lines if ln.strip()]
    rows: list[dict[str, str]] = []
    for idx, (number, line) in enumerate(lines):
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or "k" not in row or "v" not in row:
                raise ValueError("행 형식이 아니다")
            rows.append({"k": row["k"], "v": row["v"]})
        except ValueError:  # JSONDecodeError 포함
            if idx == len(lines) - 1:
                print(
                    f"경고: 임베딩 캐시의 마지막 줄({number}번째)이 잘려 있어 버린다: {path.name}",
                    file=sys.stderr,
                )
                return rows, True
            raise CacheCorruptError(
                f"임베딩 캐시 {path.name}의 {number}번째 줄이 손상됐다"
            ) from None
    return rows, False


def read_cached_keys(directory: Path, model_id: str) -> set[str]:
    """캐시 파일의 키 집합. 파일이 없으면 빈 집합이다(폴더를 만들지 않는다. dry-run용)."""
    path = cache_path(directory, model_id)
    if not path.exists():
        return set()
    return {row["k"] for row in _load_rows(path)[0]}


def cache_path(directory: Path, model_id: str) -> Path:
    return directory / f"{_SAFE.sub('_', model_id)}.jsonl"


class CachingEmbedder:
    """`Embedder`를 감싸 캐시를 쓴다. 이 클래스도 `Embedder` 프로토콜을 따른다."""

    def __init__(
        self,
        inner: Embedder,
        directory: Path,
        *,
        max_cost_usd: float | None = None,
        price_per_mtok: float | None = None,
    ) -> None:
        self._inner = inner
        self._path = cache_path(directory, inner.model_id)
        self._max_cost = max_cost_usd
        self._price = price_per_mtok
        self._vectors: dict[str, str] = {}
        self.hits = 0
        self.misses = 0
        self.est_tokens = 0  # 미적중으로 보낸 텍스트의 추정 토큰
        self.actual_tokens = 0  # 안쪽 임베더가 알려 준 실제 토큰(알려 주지 않으면 0)
        self.api_calls = 0
        self.has_usage = False
        directory.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            rows, dropped = _load_rows(self._path)
            for row in rows:
                self._vectors[row["k"]] = row["v"]
            if dropped:  # 덧붙이기 전에 깨진 꼬리를 없앤다(안 그러면 다음 줄이 이어 붙는다)
                self._path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def path(self) -> Path:
        return self._path

    def __len__(self) -> int:
        return len(self._vectors)

    def cached(self, text: str) -> bool:
        return text_key(text) in self._vectors

    @property
    def spent_tokens(self) -> int:
        return self.actual_tokens if self.has_usage else self.est_tokens

    @property
    def cost_usd(self) -> float | None:
        return estimate_cost_usd(self.model_id, self.spent_tokens, self._price)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        keys = [text_key(t) for t in texts]
        missing: dict[str, str] = {}
        for key, text in zip(keys, texts, strict=True):
            if key not in self._vectors and key not in missing:
                missing[key] = text
        self.hits += sum(1 for k in keys if k in self._vectors)
        if missing:
            self.misses += len(missing)
            items = list(missing.items())
            for start in range(0, len(items), EMBED_BATCH_SIZE):
                await self._embed_batch(items[start : start + EMBED_BATCH_SIZE])
        return [_unpack(self._vectors[k]) for k in keys]

    async def _embed_batch(self, batch: list[tuple[str, str]]) -> None:
        """미적중 텍스트 한 묶음을 임베딩하고 바로 파일에 덧붙인다(중간에 실패해도 앞은 남는다)."""
        pending = sum(estimate_tokens(text) for _, text in batch)
        self._check_budget(pending)
        before = getattr(self._inner, "usage_tokens", None)
        calls_before = int(getattr(self._inner, "api_calls", 0))
        vectors = await self._inner.embed([text for _, text in batch])
        if len(vectors) != len(batch) or any(len(v) != EMBEDDING_DIM for v in vectors):
            raise ValueError("임베더가 돌려준 벡터 개수나 차원이 맞지 않는다")
        self.est_tokens += pending
        if before is not None:
            self.has_usage = True
            self.actual_tokens += int(getattr(self._inner, "usage_tokens", 0)) - int(before)
        self.api_calls += int(getattr(self._inner, "api_calls", 0)) - calls_before
        with self._path.open("a", encoding="utf-8") as fh:
            for (key, _), vector in zip(batch, vectors, strict=True):
                blob = _pack(vector)
                self._vectors[key] = blob
                fh.write(json.dumps({"k": key, "v": blob}) + "\n")

    def _check_budget(self, pending_tokens: int) -> None:
        if self._max_cost is None:
            return
        spent = estimate_cost_usd(self.model_id, self.spent_tokens, self._price)
        extra = estimate_cost_usd(self.model_id, pending_tokens, self._price)
        if spent is None or extra is None:
            raise BudgetExceeded("모델 단가를 몰라 비용 상한을 확인할 수 없다(--price-per-mtok)")
        if spent + extra > self._max_cost:
            raise BudgetExceeded(
                f"누적 임베딩 비용 상한 초과 예상: 지금까지 {spent:.6f}달러 + 이번 {extra:.6f}달러 "
                f"> {self._max_cost}달러"
            )

    def stats(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "cache_file_entries": len(self._vectors),
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "api_calls": self.api_calls,
            "estimated_tokens": self.est_tokens,
            "actual_tokens": self.actual_tokens if self.has_usage else None,
            "cost_usd": self.cost_usd,
            "max_cost_usd": self._max_cost,
            "est_tokens_per_char": EST_TOKENS_PER_CHAR,
        }
