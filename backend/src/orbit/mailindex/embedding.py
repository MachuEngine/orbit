"""임베더 (ADR-0019 §4). OpenAI 구현과 테스트·Sim용 결정적 가짜 구현.

- 임베딩 모델 ID는 설정값이고 청크마다 저장한다. 차원은 `EMBEDDING_DIM`(1536)이어야 한다.
- OpenAI 키는 `Authorization` 헤더에만 쓴다. 예외 메시지에는 종류와 HTTP 상태만 담고 요청·응답
  본문(메일 본문 조각 포함)은 담지 않는다.
- 메일 본문이 임베딩을 위해 OpenAI로 전송된다(ADR-0019 결과 절).
"""

import hashlib
import math
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import SecretStr

from orbit.core.config import LLMSettings, Settings
from orbit.mailindex.limits import EMBED_BATCH_SIZE, EMBEDDING_DIM
from orbit.mailindex.tokenize import tokenize

FAKE_MODEL_ID = "fake-embedding-v1"


class EmbeddingError(Exception):
    """임베딩 호출 실패. `kind`(timeout·transient·request·malformed)만 안전하게 노출한다."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


@runtime_checkable
class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """입력마다 `EMBEDDING_DIM`차원 벡터를 순서대로 돌려준다."""
        ...


class FakeEmbedder:
    """결정적 가짜 임베더. 내용어 토큰을 해시 버킷에 담은 단어 주머니 벡터(L2 정규화)다.

    같은 토큰을 공유하는 글끼리 가깝지만 의미를 이해하지는 않는다. 테스트·Sim 전용이며 검색 품질
    평가에 쓰지 않는다.
    """

    def __init__(self, model_id: str = FAKE_MODEL_ID) -> None:
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        words = tokenize(text) or [text.strip() or "<empty>"]
        for word in words:
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
            vector[slot] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:  # 부호가 상쇄된 드문 경우
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]


class NullEmbedder:
    """임베딩을 하지 않는 프로세스(API)용 자리. 부르면 오류다. 범위 관리·상태 조회만 한다."""

    @property
    def model_id(self) -> str:
        return "none"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("이 프로세스는 임베딩을 하지 않는다")


class OpenAIEmbedder:
    """OpenAI `POST /embeddings` 호출. 입력을 `EMBED_BATCH_SIZE`개씩 나눠 보낸다."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model_id: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("OpenAI API 키가 비어 있다")
        if not base_url.startswith("https://"):
            raise ValueError("base URL은 https://만 허용한다")
        self._api_key = api_key
        self._model_id = model_id
        self._url = base_url.rstrip("/") + "/embeddings"
        self._timeout = timeout_s
        self._transport = transport

    @property
    def model_id(self) -> str:
        return self._model_id

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            out += await self._batch(list(texts[i : i + EMBED_BATCH_SIZE]))
        return out

    async def _batch(self, batch: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        body = {"model": self._model_id, "input": batch, "encoding_format": "float"}
        # httpx 예외(요청 헤더에 키가 있다)가 __context__에 붙지 않게 except 밖에서 던진다.
        error: EmbeddingError | None = None
        resp: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                resp = await c.post(self._url, json=body, headers=headers)
        except httpx.TimeoutException:
            error = EmbeddingError("timeout", "임베딩 요청 시간 초과")
        except httpx.TransportError as exc:
            error = EmbeddingError("transient", f"임베딩 연결 오류 ({type(exc).__name__})")
        if error is not None or resp is None:
            raise error or EmbeddingError("transient")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise EmbeddingError("transient", f"임베딩 호출 실패: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise EmbeddingError("request", f"임베딩 호출 실패: HTTP {resp.status_code}")
        return _parse(resp, len(batch))


def _parse(resp: httpx.Response, expected: int) -> list[list[float]]:
    try:
        data: Any = resp.json()["data"]
        ordered = sorted(data, key=lambda item: int(item["index"]))
        vectors = [[float(x) for x in item["embedding"]] for item in ordered]
    except (ValueError, KeyError, TypeError):
        raise EmbeddingError("malformed", "임베딩 응답을 해석하지 못했다") from None
    if len(vectors) != expected or any(len(v) != EMBEDDING_DIM for v in vectors):
        raise EmbeddingError("malformed", "임베딩 응답의 개수나 차원이 맞지 않는다")
    return vectors


def build_embedder(settings: Settings, llm_settings: LLMSettings | None = None) -> Embedder:
    """설정에서 임베더를 만든다. openai면 `ORBIT_OPENAI_API_KEY`가 필요하다(`LLMSettings`)."""
    if settings.embedding_provider == "fake":
        return FakeEmbedder()
    if llm_settings is None or llm_settings.openai_api_key is None:
        raise ValueError("임베딩 provider가 openai이면 ORBIT_OPENAI_API_KEY가 필요하다")
    return OpenAIEmbedder(
        api_key=llm_settings.openai_api_key,
        model_id=settings.embedding_model_id,
        base_url=llm_settings.openai_base_url,
    )
