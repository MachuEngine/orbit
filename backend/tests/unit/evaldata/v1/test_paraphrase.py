"""표현 변형 파생 문항 테스트 (ADR-0026 §4). 실제 LLM은 부르지 않고 스크립트 응답을 쓴다."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from orbit.evaldata.v1 import paraphrase as pp
from orbit.evaldata.v1.realize_models import FamilyModel, default_models
from orbit.llm.interface import LLMRequestError, TransientLLMError
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage

USAGE = LLMUsage(input_tokens=800, output_tokens=60)
MODELS = default_models()
CALL_COST = MODELS["openai"].cost_usd(USAGE)


def src(i: int, family: str = "openai") -> pp.Source:
    return pp.Source(
        origin_id=f"t-q-{i:04d}",
        question=f"11월 {i}일 송유진님과 잡힌 일정 전부 알려줘",
        keep=(f"11월 {i}일", "송유진"),
        family=family,
    )


def reply(text: str | None, *, finish: str = "stop", model: str = "m") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=(),
        finish_reason=finish,  # type: ignore[arg-type]
        usage=USAGE,
        cost_usd=None,
        latency_ms=1,
        model_id=model,
        provider=None,
    )


Handler = Callable[[str, int, LLMRequest], LLMResponse | Exception]


class FakeLLM:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[LLMRequest] = []
        self.calls: dict[str, int] = {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        origin = request.metadata["origin_id"]
        n = self.calls.get(origin, 0)
        self.calls[origin] = n + 1
        out = self.handler(origin, n, request)
        if isinstance(out, Exception):
            raise out
        return out


def good(i: int) -> str:
    return f"혹시 11월 {i}일에 송유진님과 잡힌 일정을 전부 알려줄래?"


def factory_for(llm: FakeLLM) -> Callable[[FamilyModel], FakeLLM]:
    return lambda model: llm


def cfg(cap: float = 1.0, concurrency: int = 2) -> pp.ParaphraseConfig:
    return pp.ParaphraseConfig(max_cost_usd=cap, concurrency=concurrency, transient_backoff_seconds=0)


async def no_sleep(_: float) -> None:
    return None


async def run(
    candidates: list[pp.Source], need: int, out: Path, llm: FakeLLM, config: pp.ParaphraseConfig
) -> pp.RunSummary:
    return await pp.run_paraphrases(
        candidates, need, out, MODELS, config, llm_factory=factory_for(llm), sleep=no_sleep  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- 검증


def test_check_accepts_a_faithful_rewrite() -> None:
    s = src(5)
    result = pp.check_paraphrase(s, good(5))
    assert result.ok and result.text == good(5)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        ("혹시 11월 5일에 일정 전부 알려줄래?", "missing_required"),  # 이름이 빠졌다
        ("혹시 송유진님과 잡힌 일정 전부 알려줄래?", "missing_required"),  # 날짜가 빠졌다
        ("11월 5일 송유진님과 잡힌 일정 전부 알려줘\n그리고 하나 더", "multiline"),
        ("11월 5일 송유진님과 잡힌 일정 전부 알려줘", "same_as_original"),
        ("11월 5일 송유진님과 슬랙으로 잡힌 일정 전부 알려줘", "real_brand"),
        ("11월 5일 송유진님과 잡힌 일정 전부 알려줘 https://x.example.com", "url"),
        ("11월 5일 송유진님과 잡힌 일정 전부 알려줘 010-1234-5678", "phone"),
        ("질문: 11월 5일 송유진님과 잡힌 일정 전부 알려줄래", "meta_prefix"),
        ("“11월 5일 송유진님과 잡힌 일정 전부 알려줄래?”", "wrapped_quotes"),
        ("11월 5일 송유진님과 잡힌 일정 전부 알려줘 " + "아주 " * 60, "too_long"),
    ],
)
def test_check_rejects_with_a_reason_code(text: str, reason: str) -> None:
    result = pp.check_paraphrase(src(5), text)
    assert not result.ok and reason in result.reasons


def test_check_failure_reasons_do_not_contain_the_text() -> None:
    result = pp.check_paraphrase(src(5), "슬랙으로 알려줘 SECRET-MARKER")
    assert "SECRET-MARKER" not in json.dumps(result.reasons, ensure_ascii=False)


def test_prompt_lists_keep_tokens_and_hints() -> None:
    s = src(5)
    text = pp.build_user(s, ["너무 길었다"])
    assert "송유진" in text and "11월 5일" in text and "너무 길었다" in text
    assert s.question in text


# ---------------------------------------------------------------- 실행


async def test_retries_a_failed_attempt_and_stores_only_the_valid_sentence(tmp_path: Path) -> None:
    def handler(origin: str, n: int, req: LLMRequest) -> LLMResponse:
        return reply("이름 없는 문장" if n == 0 else good(1))

    llm = FakeLLM(handler)
    summary = await run([src(1)], 1, tmp_path, llm, cfg())
    assert (summary.ok, summary.failed, summary.requests) == (1, 0, 2)
    (row,) = pp.read_all_rows(tmp_path / pp.PARAPHRASE_FILE)
    assert row["status"] == "ok" and row["text"] == good(1) and row["attempts"] == 2
    assert "missing_required" in row["failure_reasons"][0]
    # 실패한 응답 원문은 저장하지 않는다
    assert "이름 없는 문장" not in (tmp_path / pp.PARAPHRASE_FILE).read_text(encoding="utf-8")
    # 재시도 요청에는 이전 실패 사유가 들어간다
    assert "필수 사실이 빠졌다" in str(llm.requests[1].messages[1].content)


async def test_gives_up_after_three_attempts_and_replaces_with_next_candidate(
    tmp_path: Path,
) -> None:
    def handler(origin: str, n: int, req: LLMRequest) -> LLMResponse:
        if origin == "t-q-0001":
            return reply("항상 실패하는 문장")
        return reply(good(int(origin[-4:])))

    llm = FakeLLM(handler)
    candidates = [src(1), src(2), src(3)]
    summary = await run(candidates, 2, tmp_path, llm, cfg(concurrency=1))
    assert summary.failed == 1 and summary.ok == 2
    assert llm.calls["t-q-0001"] == 3  # 최초 1회 + 재시도 2회 = 3회 시도
    rows = pp.read_rows(tmp_path / pp.PARAPHRASE_FILE)
    assert rows["t-q-0001"]["status"] == "failed" and rows["t-q-0001"]["text"] is None
    # 실패한 원본은 건너뛰고 다음 후보로 채운다
    assert [s.origin_id for s in candidates if rows[s.origin_id]["status"] == "ok"] == [
        "t-q-0002",
        "t-q-0003",
    ]


async def test_resume_skips_finished_candidates_without_calling_the_llm(tmp_path: Path) -> None:
    first = FakeLLM(lambda o, n, r: reply(good(int(o[-4:]))))
    await run([src(1), src(2)], 2, tmp_path, first, cfg())
    second = FakeLLM(lambda o, n, r: AssertionError("호출하면 안 된다"))
    summary = await run([src(1), src(2)], 2, tmp_path, second, cfg())
    assert summary.requests == 0 and second.requests == []
    assert len(pp.read_all_rows(tmp_path / pp.PARAPHRASE_FILE)) == 2


async def test_changed_source_text_invalidates_the_stored_row(tmp_path: Path) -> None:
    llm = FakeLLM(lambda o, n, r: reply(good(1)))
    await run([src(1)], 1, tmp_path, llm, cfg())
    edited = pp.Source("t-q-0001", "11월 1일 송유진님 일정 알려줘", ("11월 1일", "송유진"), "openai")
    rows = pp.read_rows(tmp_path / pp.PARAPHRASE_FILE)
    assert pp.usable_row(rows["t-q-0001"], src(1))
    assert not pp.usable_row(rows["t-q-0001"], edited)


async def test_cost_cap_stops_new_requests_and_records_aborted_rows(tmp_path: Path) -> None:
    llm = FakeLLM(lambda o, n, r: reply("항상 실패"))  # 계속 실패해 비용만 쌓인다
    cap = CALL_COST * 1.5
    summary = await run([src(i) for i in range(1, 6)], 5, tmp_path, llm, cfg(cap=cap, concurrency=1))
    assert summary.stop_reason == "cost_cap"
    assert summary.requests == 2  # 상한을 넘긴 호출 뒤에는 새 요청을 하지 않는다
    rows = pp.read_all_rows(tmp_path / pp.PARAPHRASE_FILE)
    assert [r["status"] for r in rows] == ["aborted"]
    assert rows[0]["cost_usd"] == pytest.approx(CALL_COST * 2, rel=1e-6)
    assert pp.total_cost(rows) == pytest.approx(summary.spent_usd, rel=1e-6)
    # 중단된 원본은 다시 시도한다
    again = FakeLLM(lambda o, n, r: reply(good(int(o[-4:]))))
    resumed = await run([src(i) for i in range(1, 3)], 2, tmp_path, again, cfg(cap=1.0))
    assert resumed.ok == 2
    assert pp.total_cost(pp.read_all_rows(tmp_path / pp.PARAPHRASE_FILE)) > summary.spent_usd


async def test_reported_cost_larger_than_computed_is_charged(tmp_path: Path) -> None:
    expensive = LLMResponse(
        text=good(1), tool_calls=(), finish_reason="stop", usage=USAGE, cost_usd=0.5,
        latency_ms=1, model_id="m", provider=None,
    )  # fmt: skip
    summary = await run([src(1)], 1, tmp_path, FakeLLM(lambda o, n, r: expensive), cfg())
    assert summary.spent_usd == pytest.approx(0.5)


async def test_permanent_error_stops_with_fatal(tmp_path: Path) -> None:
    llm = FakeLLM(lambda o, n, r: LLMRequestError("잘못된 요청"))
    summary = await run([src(1), src(2)], 2, tmp_path, llm, cfg(concurrency=1))
    assert summary.stop_reason == "fatal" and "영구 오류" in (summary.fatal_message or "")
    assert [r["status"] for r in pp.read_all_rows(tmp_path / pp.PARAPHRASE_FILE)] == ["aborted"]


async def test_transient_error_is_retried_then_succeeds(tmp_path: Path) -> None:
    def handler(origin: str, n: int, req: LLMRequest) -> LLMResponse | Exception:
        return TransientLLMError("일시 오류") if n == 0 else reply(good(1))

    summary = await run([src(1)], 1, tmp_path, FakeLLM(handler), cfg())
    assert summary.ok == 1 and summary.requests == 2


async def test_each_family_uses_its_own_model_settings(tmp_path: Path) -> None:
    seen: dict[str, FamilyModel] = {}

    def factory(model: FamilyModel) -> FakeLLM:
        seen[model.family] = model
        return FakeLLM(lambda o, n, r: reply(good(int(o[-4:]))))

    cands = [src(1, "openai"), src(2, "anthropic"), src(3, "google")]
    await pp.run_paraphrases(
        cands, 3, tmp_path, MODELS, cfg(), llm_factory=factory, sleep=no_sleep  # type: ignore[arg-type]
    )
    assert set(seen) == {"openai", "anthropic", "google"}
    rows = pp.read_rows(tmp_path / pp.PARAPHRASE_FILE)
    assert {r["family"] for r in rows.values()} == {"openai", "anthropic", "google"}
    assert seen["anthropic"].temperature is None  # Anthropic은 temperature를 보내지 않는다


async def test_anthropic_request_omits_temperature(tmp_path: Path) -> None:
    llm = FakeLLM(lambda o, n, r: reply(good(2)))
    await run([src(2, "anthropic")], 1, tmp_path, llm, cfg())
    assert llm.requests[0].temperature is None
    assert llm.requests[0].purpose == "data_generation"


def test_estimate_scales_with_sources_and_families() -> None:
    one = pp.estimate_cost([src(1)], MODELS)
    three = pp.estimate_cost([src(1), src(2), src(3, "google")], MODELS)
    assert three["total_cost_usd"] > one["total_cost_usd"] > 0
    assert set(three["families"]) == {"openai", "google"}


def test_read_rows_ignores_a_truncated_last_line(tmp_path: Path) -> None:
    path = tmp_path / pp.PARAPHRASE_FILE
    path.write_text('{"origin_id": "a", "status": "ok"}\n{"origin_id": "b", "sta', encoding="utf-8")
    assert set(pp.read_rows(path)) == {"a"}
    writer = pp._Writer(path)  # noqa: SLF001  끊긴 줄 뒤에서 새 줄로 시작한다
    writer.write({"origin_id": "c", "status": "ok"})
    writer.close()
    assert set(pp.read_rows(path)) == {"a", "c"}
