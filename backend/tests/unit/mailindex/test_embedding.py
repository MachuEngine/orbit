"""임베더 (ADR-0019 §4): 결정적 가짜 구현과 OpenAI 호출 형식·오류 처리. 실제 호출은 없다."""

import json
import math

import httpx
import pytest
from pydantic import SecretStr

from orbit.core.config import LLMSettings, Settings
from orbit.mailindex.embedding import (
    FAKE_MODEL_ID,
    EmbeddingError,
    FakeEmbedder,
    OpenAIEmbedder,
    build_embedder,
)
from orbit.mailindex.limits import EMBED_BATCH_SIZE, EMBEDDING_DIM

KEY = "sk-test-synthetic-key-0000"  # gitleaks:allow (합성 테스트 값)


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_fake_embedder_is_deterministic_and_normalized() -> None:
    e = FakeEmbedder()
    [a], [b] = await e.embed(["견적 납기 문의"]), await e.embed(["견적 납기 문의"])
    assert a == b and len(a) == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-9)
    assert e.model_id == FAKE_MODEL_ID


async def test_fake_embedder_puts_shared_words_closer() -> None:
    e = FakeEmbedder()
    q, near, far = await e.embed(["견적 납기", "견적 단가 납기 협의", "점심 메뉴 안내"])
    assert _cos(q, near) > _cos(q, far)
    [empty] = await e.embed([""])  # 내용어가 없어도 0벡터가 되지 않는다
    assert any(empty)


def _ok(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    # 일부러 거꾸로 돌려줘 index로 정렬하는지 본다.
    data = [
        {"index": i, "embedding": [float(i)] + [0.0] * (EMBEDDING_DIM - 1)}
        for i in reversed(range(len(inputs)))
    ]
    return httpx.Response(200, json={"data": data})


async def test_openai_embedder_batches_and_orders_and_sends_key_only_in_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    e = OpenAIEmbedder(
        api_key=SecretStr(KEY),
        model_id="text-embedding-3-small",
        transport=httpx.MockTransport(handler),
    )
    texts = [f"합성 문장 {i}" for i in range(EMBED_BATCH_SIZE + 3)]
    vectors = await e.embed(texts)
    assert len(vectors) == len(texts)
    assert len(seen) == 2  # 64개 + 3개
    assert vectors[0][0] == 0.0 and vectors[1][0] == 1.0 and vectors[EMBED_BATCH_SIZE][0] == 0.0
    body = json.loads(seen[0].content)
    assert body["model"] == "text-embedding-3-small" and len(body["input"]) == EMBED_BATCH_SIZE
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in seen[0].content.decode()
    assert str(seen[0].url) == "https://api.openai.com/v1/embeddings"


@pytest.mark.parametrize(
    ("status", "kind"), [(429, "transient"), (500, "transient"), (400, "request"), (401, "request")]
)
async def test_openai_errors_carry_kind_and_status_only(status: int, kind: str) -> None:
    body = {"error": {"message": f"echo of {KEY} and 합성 본문"}}
    e = OpenAIEmbedder(
        api_key=SecretStr(KEY),
        model_id="m",
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)),
    )
    with pytest.raises(EmbeddingError) as info:
        await e.embed(["합성 본문"])
    assert info.value.kind == kind
    assert KEY not in str(info.value) and "합성" not in str(info.value)


async def test_openai_timeout_and_malformed_response() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("t", request=request)

    slow = OpenAIEmbedder(api_key=SecretStr(KEY), model_id="m", transport=httpx.MockTransport(boom))
    with pytest.raises(EmbeddingError) as info:
        await slow.embed(["x"])
    assert info.value.kind == "timeout"

    short = OpenAIEmbedder(
        api_key=SecretStr(KEY),
        model_id="m",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
        ),
    )
    with pytest.raises(EmbeddingError) as info2:
        await short.embed(["x"])
    assert info2.value.kind == "malformed"


def test_openai_embedder_rejects_blank_key_and_plain_http() -> None:
    with pytest.raises(ValueError, match="키"):
        OpenAIEmbedder(api_key=SecretStr(" "), model_id="m")
    with pytest.raises(ValueError, match="https"):
        OpenAIEmbedder(api_key=SecretStr(KEY), model_id="m", base_url="http://x.test/v1")


def _settings(**kw: object) -> Settings:
    return Settings(database_url=SecretStr("postgresql://x"), **kw)  # type: ignore[arg-type]


def test_build_embedder_by_setting() -> None:
    assert isinstance(build_embedder(_settings(embedding_provider="fake")), FakeEmbedder)
    llm = LLMSettings(llm_model_id="openai/m", llm_provider="openai", openai_api_key=SecretStr(KEY))
    built = build_embedder(_settings(embedding_model_id="text-embedding-3-small"), llm)
    assert isinstance(built, OpenAIEmbedder) and built.model_id == "text-embedding-3-small"
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_embedder(_settings())
