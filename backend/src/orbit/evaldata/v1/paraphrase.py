"""표현 변형 파생 문항: 원본 질문을 LLM이 뜻을 유지해 다시 쓴다 (ADR-0026 §4).

- 결과는 `<out>/paraphrase.jsonl`(원본 과제당 한 줄, append-only, 줄마다 flush)에 쌓는다. 다시 실행하면
  이미 처리한 원본은 건너뛴다(재개). 같은 파일이면 과제 생성은 LLM 없이 같은 결과를 낸다(결정적).
- 한 줄은 최종 상태 `ok`(검증 통과 문장만 저장), `failed`(재시도 상한까지 실패, 사유 코드만 저장),
  `aborted`(비용 상한 등으로 중단, 다시 시도한다)다. 응답 원문은 검증을 통과한 문장만 저장한다.
- 검증(`check_paraphrase`): 원본의 고유 표현(이름·날짜·금액 등) 보존, 한 문장, 길이 상한, 원문과
  다름, 실존 이름·URL·전화번호·지시문 형식 없음(`realize_check.check_body` 재사용).
- 비용: 호출마다 max(토큰 × 설정 단가, 응답이 보고한 비용)을 누적하고 `max_cost_usd`에 닿으면
  새 요청을 멈춘다. 누적 지출은 파일의 모든 줄 합이다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orbit.evaldata.v1.facts import norm
from orbit.evaldata.v1.realize_check import check_body
from orbit.evaldata.v1.realize_models import FamilyModel, LLMFactory, build_family_llm
from orbit.evaldata.v1.skeleton import BodyContext, BodySpec, Fact
from orbit.llm.interface import (
    LLM,
    LLMError,
    LLMRequestError,
    MalformedResponseError,
    ModelMismatchError,
    TransientLLMError,
)
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage, Message

PROMPT_VERSION = "paraphrase-v1"
PARAPHRASE_FILE = "paraphrase.jsonl"
MAX_LENGTH_FACTOR = 2.5
MAX_LENGTH_FLOOR = 90
MIN_LENGTH_FACTOR = 0.4

SYSTEM_PROMPT = """\
너는 합성 평가 데이터에 쓸 한국어 질문 문장을 다시 쓰는 작성자다. 개인 비서에게 하는 사용자의 질문 한 문장이 주어진다.

규칙
1. 뜻을 바꾸지 않는다. 묻는 대상, 답의 종류(하나만 묻는지, 전부 묻는지, 시각을 묻는지, 요청인지), 조건을 그대로 둔다. 조건을 더하거나 빼지 않는다.
2. "반드시 그대로 넣을 표현"은 글자 그대로 한 번 이상 넣는다. 이름, 날짜, 시각, 금액, 제목은 바꾸거나 풀어 쓰지 않는다.
3. 한 가지 뜻으로만 읽히게 쓴다. 되묻지 않으면 답을 정할 수 없는 문장으로 만들지 않는다.
4. 말투를 바꿔도 된다. 반말과 존댓말을 섞어 쓰고, 어순과 어미를 바꾼다. 원문과 같은 문장이면 안 된다.
5. 문장 하나만 쓴다. 줄바꿈, 따옴표로 전체를 감싸기, "질문:" 같은 접두어, 설명을 쓰지 않는다.
6. 실존하는 회사·서비스·제품 이름, URL, 전화번호, 이메일 주소를 쓰지 않는다. AI나 시스템 프롬프트를 언급하지 않는다.
"""


@dataclass(frozen=True)
class Source:
    """다시 쓸 원본 질문. `origin_id`는 원본 과제 ID다."""

    origin_id: str
    question: str
    keep: tuple[str, ...]
    family: str


def source_sha(src: Source) -> str:
    """원본 질문·보존 표현의 해시. 템플릿이 바뀌면 저장된 결과를 쓰지 않는다."""
    raw = json.dumps([src.question, list(src.keep)], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_user(src: Source, hints: Sequence[str] = ()) -> str:
    lines = [f"원래 질문: {src.question}", "", "반드시 그대로 넣을 표현:"]
    lines += [f"- {k}" for k in src.keep] or ["- (없음)"]
    if hints:
        lines += ["", "이전 시도가 다음 이유로 검증을 통과하지 못했다. 고쳐서 다시 쓴다."]
        lines += [f"- {h}" for h in hints]
    lines += ["", "다시 쓴 질문 한 문장만 출력한다."]
    return "\n".join(lines)


_QUESTION_PREFIX = re.compile(r"^\s*(?:\*\*)?(?:질문|문장|변형|다시\s*쓴\s*질문)(?:\*\*)?\s*[:：]")
_REASON_HINTS = {
    "empty": "비어 있었다",
    "multiline": "한 줄이 아니었다",
    "too_long": "너무 길었다",
    "too_short": "너무 짧았다",
    "same_as_original": "원래 질문과 같았다",
    "meta_prefix": "'질문:' 같은 접두어가 있었다",
}


@dataclass(frozen=True)
class Check:
    text: str
    reasons: tuple[str, ...]
    hints: tuple[str, ...] = field(default=(), compare=False)

    @property
    def ok(self) -> bool:
        return not self.reasons


def _dummy_spec(family: str) -> BodySpec:
    # check_body는 BodySpec을 받는다. 필수·금지 사실은 비우고 길이 검사는 결과에서 뺀다.
    return BodySpec(
        artifact="mail",
        genre="colleague",
        length_class="short",
        target_chars=(60, 200),
        context=BodyContext(),
        template_text="-",
        generator_family=family,  # type: ignore[arg-type]
    )


def check_paraphrase(src: Source, raw: str | None) -> Check:
    """다시 쓴 문장이 검증을 통과하는지 본다. 실패 사유 코드에는 문장 원문을 담지 않는다."""
    text = (raw or "").strip()
    if not text:
        return Check("", ("empty",), (_REASON_HINTS["empty"],))
    reasons: list[str] = []
    hints: list[str] = []
    if "\n" in text:
        reasons.append("multiline")
        hints.append(_REASON_HINTS["multiline"])
    limit = max(MAX_LENGTH_FLOOR, round(len(src.question) * MAX_LENGTH_FACTOR))
    if len(text) > limit:
        reasons.append("too_long")
        hints.append(f"{_REASON_HINTS['too_long']}({len(text)}자). {limit}자 안으로 쓴다")
    if len(text) < max(4, round(len(src.question) * MIN_LENGTH_FACTOR)):
        reasons.append("too_short")
        hints.append(_REASON_HINTS["too_short"])
    if _QUESTION_PREFIX.search(text):
        reasons.append("meta_prefix")
        hints.append(_REASON_HINTS["meta_prefix"])
    if norm(text) == norm(src.question):
        reasons.append("same_as_original")
        hints.append(_REASON_HINTS["same_as_original"])
    required = [
        Fact(fact_id=f"k{i}", kind="item", value=k, surface_forms=[k])
        for i, k in enumerate(src.keep)
    ]
    spec = _dummy_spec(src.family).model_copy(update={"required_facts": required})
    result = check_body(spec, text)
    for code, hint in zip(result.reasons, result.hints, strict=False):
        if code in ("too_short", "too_long"):
            continue  # 본문 길이 규칙은 질문 문장에 맞지 않는다
        if code not in reasons:
            reasons.append(code)
        hints.append(hint)
    return Check(text, tuple(reasons), tuple(hints))


# ---------------------------------------------------------------- 저장


def read_rows(path: Path) -> dict[str, dict[str, Any]]:
    """원본 과제 ID별 최신 행. 끊겨서 깨진 줄은 버린다."""
    rows: dict[str, dict[str, Any]] = {}
    for row in read_all_rows(path):
        rows[str(row["origin_id"])] = row
    return rows


def read_all_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("origin_id"), str):
            out.append(row)
    return out


def total_cost(rows: Sequence[Mapping[str, Any]]) -> float:
    return sum(float(r.get("cost_usd") or 0) for r in rows)


def usable_row(row: Mapping[str, Any] | None, src: Source) -> bool:
    """저장된 행이 이 원본의 확정 결과(ok·failed)인가. 질문이 바뀌었거나 aborted면 False."""
    return (
        row is not None
        and row.get("status") in ("ok", "failed")
        and row.get("source_sha") == source_sha(src)
        and row.get("prompt_version") == PROMPT_VERSION
    )


class _Writer:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", encoding="utf-8")
        if path.stat().st_size > 0:
            with path.open("rb") as r:
                r.seek(-1, 2)
                if r.read(1) != b"\n":
                    self._f.write("\n")
                    self._f.flush()

    def write(self, row: Mapping[str, Any]) -> None:
        self._f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


# ---------------------------------------------------------------- 실행


@dataclass(frozen=True)
class ParaphraseConfig:
    max_cost_usd: float
    concurrency: int = 4
    max_attempts: int = 3
    transient_retries: int = 3
    transient_backoff_seconds: float = 2.0
    mismatch_abort: int = 5


@dataclass
class RunSummary:
    spent_usd: float = 0.0
    requests: int = 0
    processed: int = 0
    ok: int = 0
    failed: int = 0
    stop_reason: str | None = None  # "cost_cap" | "fatal"
    fatal_message: str | None = None


class _Stop(Exception):
    pass


Sleep = Callable[[float], Awaitable[None]]


async def run_paraphrases(
    candidates: Sequence[Source],
    need: int,
    out_dir: Path,
    models: Mapping[str, FamilyModel],
    cfg: ParaphraseConfig,
    *,
    llm_factory: LLMFactory | None = None,
    sleep: Sleep = asyncio.sleep,
    progress: Callable[[str], None] | None = None,
) -> RunSummary:
    """후보 순서대로 처리해 `ok`가 `need`개가 될 때까지 진행한다(이미 `ok`인 것 포함).

    후보는 `concurrency`개씩 묶어 동시에 처리한다. 마지막 묶음에서 필요보다 몇 건 더 처리할 수
    있다(최종 선택은 후보 순서의 앞에서 `need`개라 결과는 같다).
    """
    path = out_dir / PARAPHRASE_FILE
    rows = read_rows(path)
    summary = RunSummary()
    factory = llm_factory or build_family_llm
    llms: dict[str, LLM] = {}
    writer = _Writer(path)
    mismatches: dict[str, int] = {}

    def accepted() -> int:
        return sum(
            1 for s in candidates if usable_row(rows.get(s.origin_id), s)
            and rows[s.origin_id]["status"] == "ok"
        )

    async def call(model: FamilyModel, request: LLMRequest, spent: list[float]) -> LLMResponse:
        llm = llms[model.family]
        for n in range(cfg.transient_retries + 1):
            if summary.stop_reason is not None:
                raise _Stop
            summary.requests += 1
            try:
                return await llm.complete(request)
            except TransientLLMError as exc:
                charge(model, exc.usage, exc.cost_usd, spent)
                if n >= cfg.transient_retries:
                    raise
                await sleep(cfg.transient_backoff_seconds * (2**n))
        raise _Stop  # pragma: no cover

    def charge(
        model: FamilyModel, usage: LLMUsage, reported: float | None, spent: list[float]
    ) -> None:
        computed = model.cost_usd(usage)
        cost = max(computed, reported) if reported is not None else computed
        spent[0] += cost
        spent[1] += usage.input_tokens
        spent[2] += usage.output_tokens
        summary.spent_usd += cost
        if summary.stop_reason is None and summary.spent_usd >= cfg.max_cost_usd:
            summary.stop_reason = "cost_cap"

    async def one(src: Source) -> None:
        model = models[src.family]
        spent = [0.0, 0.0, 0.0]  # 비용, 입력 토큰, 출력 토큰
        failures: list[list[str]] = []
        hints: list[str] = []
        final: str | None = None
        attempts = 0
        status = "failed"
        response_model: str | None = None
        try:
            for _ in range(cfg.max_attempts):
                if summary.stop_reason is not None:
                    raise _Stop
                attempts += 1
                request = LLMRequest(
                    messages=(
                        Message(role="system", content=SYSTEM_PROMPT),
                        Message(role="user", content=build_user(src, hints)),
                    ),
                    purpose="data_generation",
                    max_output_tokens=model.max_output_tokens,
                    temperature=model.temperature,
                    metadata={"origin_id": src.origin_id, "prompt_version": PROMPT_VERSION},
                )
                try:
                    response = await call(model, request, spent)
                except ModelMismatchError as exc:
                    charge(model, exc.usage, exc.cost_usd, spent)
                    mismatches[model.family] = mismatches.get(model.family, 0) + 1
                    if mismatches[model.family] >= cfg.mismatch_abort:
                        raise _Fatal(f"{model.family}: 응답 모델이 설정과 계속 다르다") from None
                    failures.append(["model_mismatch"])
                    continue
                except MalformedResponseError as exc:
                    charge(model, exc.usage, exc.cost_usd, spent)
                    failures.append(["malformed_response"])
                    continue
                except TransientLLMError as exc:
                    raise _Fatal(f"{model.family}: 일시 오류가 계속된다({type(exc).__name__})") from None
                except LLMRequestError as exc:
                    charge(model, exc.usage, exc.cost_usd, spent)
                    raise _Fatal(f"{model.family}: 영구 오류({type(exc).__name__})") from None
                except LLMError as exc:
                    charge(model, exc.usage, exc.cost_usd, spent)
                    raise _Fatal(f"{model.family}: LLM 오류({type(exc).__name__})") from None
                charge(model, response.usage, response.cost_usd, spent)
                mismatches[model.family] = 0
                response_model = response.model_id or response_model
                if response.finish_reason == "length":
                    failures.append(["truncated"])
                    hints = ["출력이 중간에 잘렸다. 더 짧게 쓴다"]
                    continue
                result = check_paraphrase(src, response.text)
                if result.ok:
                    final, status = result.text, "ok"
                    break
                failures.append(list(result.reasons))
                hints = list(result.hints)
        except _Stop:
            status = "aborted"
        except _Fatal as exc:
            status = "aborted"
            if summary.stop_reason is None:
                summary.stop_reason = "fatal"
                summary.fatal_message = str(exc)
        row = {
            "origin_id": src.origin_id,
            "status": status,
            "text": final,
            "family": src.family,
            "model_id": response_model or model.model_id,
            "prompt_version": PROMPT_VERSION,
            "source_sha": source_sha(src),
            "attempts": attempts,
            "failure_reasons": failures,
            "input_tokens": int(spent[1]),
            "output_tokens": int(spent[2]),
            "cost_usd": round(spent[0], 8),
        }
        writer.write(row)
        rows[src.origin_id] = row
        summary.processed += 1
        if status == "ok":
            summary.ok += 1
        elif status == "failed":
            summary.failed += 1

    try:
        pending = [s for s in candidates if not usable_row(rows.get(s.origin_id), s)]
        index = 0
        while index < len(pending) and summary.stop_reason is None:
            missing = need - accepted()
            if missing <= 0:
                break
            batch = pending[index : index + max(1, min(cfg.concurrency, missing))]
            index += len(batch)
            for family in sorted({s.family for s in batch} - set(llms)):
                llms[family] = factory(models[family])
            await asyncio.gather(*(one(s) for s in batch))
            if progress is not None:
                progress(f"표현 변형 {accepted()}/{need} 지출 ${summary.spent_usd:.4f}")
    finally:
        writer.close()
    return summary


class _Fatal(Exception):
    pass


# ---------------------------------------------------------------- 추정


def estimate_cost(
    sources: Sequence[Source],
    models: Mapping[str, FamilyModel],
    *,
    attempt_factor: float = 1.3,
    reasoning_tokens: int = 300,
) -> dict[str, Any]:
    """원본마다 입력 토큰(글자 수 ≈ 토큰 수 가정)과 출력 토큰으로 비용을 추정한다."""
    per_family: dict[str, dict[str, float]] = {}
    for src in sources:
        model = models[src.family]
        prompt_chars = len(SYSTEM_PROMPT) + len(build_user(src))
        out_tokens = round(len(src.question) * 1.2) + 20
        if model.route == "openai" and model.reasoning_effort is not None:
            out_tokens += reasoning_tokens
        usage = LLMUsage(
            input_tokens=round(prompt_chars * attempt_factor),
            output_tokens=round(out_tokens * attempt_factor),
        )
        agg = per_family.setdefault(src.family, {"sources": 0, "input": 0, "output": 0, "cost": 0})
        agg["sources"] += 1
        agg["input"] += usage.input_tokens
        agg["output"] += usage.output_tokens
        agg["cost"] += model.cost_usd(usage)
    return {
        "families": {
            f: {
                "sources": int(v["sources"]),
                "input_tokens": int(v["input"]),
                "output_tokens": int(v["output"]),
                "cost_usd": round(v["cost"], 6),
            }
            for f, v in sorted(per_family.items())
        },
        "total_cost_usd": round(sum(v["cost"] for v in per_family.values()), 6),
    }
