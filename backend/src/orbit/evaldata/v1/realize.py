"""본문 실현기: 뼈대의 본문 명세마다 LLM으로 본문을 한 번 쓰고 검증해 저장한다 (ADR-0026 §2).

```bash
cd backend
uv run python -m orbit.evaldata.v1.realize --skeleton /tmp/w1 --out /tmp/w1 \
  --max-cost-usd 3 --limit 90 --sample-seed 1 [--families openai,google] [--dry-run] [--assemble]
```

- 결과는 `<out>/bodies.jsonl`(body_id당 한 줄, append-only, 줄마다 flush)에 쌓는다. 다시 실행하면
  이미 성공했거나 템플릿으로 대체한 body_id는 건너뛴다. 실패한 시도는 사유 코드만 남기고 응답 원문은
  저장하지 않는다.
- `<out>/realize-manifest.json`에 모델·단가·시드·계열별 집계·비용을 남긴다.
- `--assemble`이면 `<out>/world.json`(Sim 세계 스냅샷)을 만든다.
- `--assemble --allow-skeleton-change`: skeleton이 바뀌었어도 `bodies` 구역(본문 명세)이 같으면 이미
  만든 본문으로 `world.json`을 다시 조립한다(ADR-0027, LLM을 부르지 않는다). 본문 명세가 하나라도
  다르면 거부하고 다른 body_id 수를 출력한다. 이전 매니페스트에 `bodies_sha256`이 없으면
  `--previous-skeleton`으로 이전 skeleton을 준다(sha가 매니페스트와 같아야 한다). 매니페스트에는
  이전·새 skeleton sha와 bodies 해시를 남긴다.

비용 상한: `--max-cost-usd`는 이번 실행, `--max-total-usd`(선택)는 출력 디렉터리 전체 누적 지출
(bodies.jsonl 행 합과 매니페스트 runs 합 중 큰 쪽)에 건다. 먼저 닿는 쪽에서 멈춘다.

종료 코드: 0 정상, 2 입력·설정 오류, 3 비용 상한 도달(결과는 저장됨), 4 영구 오류로 중단,
5 조립 불가(본문이 비어 있고 `--allow-template-missing`이 없음).

한계: 동시 요청 때문에 상한을 넘긴 요청 말고도 진행 중이던 최대 (동시 요청 수 - 1)건의 비용만큼
더 넘을 수 있다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.v1.build import dump_json
from orbit.evaldata.v1.export import export_world
from orbit.evaldata.v1.realize_check import check_body
from orbit.evaldata.v1.realize_models import (
    FAMILIES,
    FamilyModel,
    LLMFactory,
    ModelConfigError,
    build_family_llm,
    load_models,
)
from orbit.evaldata.v1.realize_prompt import PROMPT_VERSION, build_system, build_user
from orbit.evaldata.v1.skeleton import SCHEMA_VERSION, BodySpec, Skeleton
from orbit.llm.interface import (
    LLM,
    LLMError,
    LLMRequestError,
    MalformedResponseError,
    ModelMismatchError,
    TransientLLMError,
)
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage, Message

BODIES_FILE = "bodies.jsonl"
MANIFEST_FILE = "realize-manifest.json"
WORLD_FILE = "world.json"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_COST_CAP = 3
EXIT_FATAL = 4
EXIT_CANNOT_ASSEMBLE = 5

Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]


@dataclass(frozen=True)
class RealizeConfig:
    max_cost_usd: float
    max_total_usd: float | None = None  # 출력 디렉터리 전체 누적 상한(이전 실행 지출 포함)
    concurrency: int = 8
    max_attempts: int = 4  # 최초 1 + 재생성 3
    transient_retries: int = 4  # 어댑터 재시도 뒤에도 남는 일시 오류를 다시 시도하는 횟수
    transient_backoff_seconds: float = 2.0
    mismatch_abort: int = 5  # 한 계열에서 응답 모델 불일치가 이만큼 연속되면 설정 오류로 보고 중단
    families: tuple[str, ...] = FAMILIES
    limit: int | None = None
    sample_seed: int = 0


# ---------------------------------------------------------------- 선택·재개


def select_ids(
    skeleton: Skeleton, families: tuple[str, ...], limit: int | None, sample_seed: int
) -> list[str]:
    """실행 대상 body_id. `limit`이 없으면 선택한 계열의 전부, 있으면 층화 표본이다.

    표본은 계열마다 limit을 나눠(나머지는 계열 이름순으로 한 건씩) (artifact, genre, length_class)
    층을 돌아가며 뽑는다. 같은 입력이면 항상 같은 결과다(저장된 진행 상황과 무관).
    """
    chosen_families = sorted(set(families))
    if limit is None:
        return sorted(b for b, s in skeleton.bodies.items() if s.generator_family in families)
    base, extra = divmod(limit, len(chosen_families))
    out: list[str] = []
    for i, family in enumerate(chosen_families):
        quota = base + (1 if i < extra else 0)
        strata: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for bid, spec in sorted(skeleton.bodies.items()):
            if spec.generator_family == family:
                strata[(spec.artifact, spec.genre, spec.length_class)].append(bid)
        queues: list[list[str]] = []
        for key in sorted(strata):
            ids = strata[key]
            seed = hashlib.sha256(f"{sample_seed}:{family}:{key}".encode()).hexdigest()
            random.Random(seed).shuffle(ids)  # noqa: S311 - 표본 추출용 결정적 난수
            queues.append(ids)
        picked: list[str] = []
        while len(picked) < quota and any(queues):
            for q in queues:
                if q and len(picked) < quota:
                    picked.append(q.pop())
        out.extend(picked)
    return sorted(out)


def read_all_rows(path: Path) -> list[dict[str, Any]]:
    """`bodies.jsonl`의 모든 줄(지출 합계용). 끊겨서 깨진 줄은 버린다."""
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
        if isinstance(row, dict) and isinstance(row.get("body_id"), str):
            out.append(row)
    return out


def read_rows(path: Path) -> dict[str, dict[str, Any]]:
    """body_id별 최신 행. 파일은 append-only라 같은 body_id가 여러 줄이면 나중 줄이 이긴다.

    같은 body_id가 여러 줄인 경우는 `--recheck`로 다시 만든 본문이다.

    끊겨서 깨진 마지막 줄은 버린다(다시 생성한다).
    """
    rows: dict[str, dict[str, Any]] = {}
    for row in read_all_rows(path):
        rows[row["body_id"]] = row
    return rows


class _RowWriter:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", encoding="utf-8")
        if path.stat().st_size > 0 and not _ends_with_newline(path):
            self._f.write("\n")  # 끊긴 줄 뒤에서 새 줄로 시작한다
            self._f.flush()

    def write(self, row: Mapping[str, Any]) -> None:
        self._f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as f:
        f.seek(-1, 2)
        return f.read(1) == b"\n"


# ---------------------------------------------------------------- 생성


class _Fatal(Exception):
    """영구 오류. 새 요청을 멈춘다."""


@dataclass
class _State:
    spent: float = 0.0
    requests: int = 0
    done_bodies: int = 0
    stop_reason: str | None = None  # "cost_cap" | "total_cap" | "fatal"
    prior_spent: float = 0.0
    zero_usage: int = 0
    fatal_message: str | None = None
    prior_template_used: int = 0
    transient_retries: int = 0
    consecutive_mismatch: dict[str, int] = field(default_factory=dict)

    @property
    def stopped(self) -> bool:
        return self.stop_reason is not None


@dataclass
class _Attempts:
    attempts: int = 0
    failures: list[list[str]] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    cost: float = 0.0  # 누적에 넣은 값(호출마다 max(계산값, 보고값))
    cost_computed: float = 0.0
    cost_reported: float | None = None
    transient_retries: int = 0
    response_model: str | None = None


class _Realizer:
    def __init__(
        self,
        skeleton: Skeleton,
        models: Mapping[str, FamilyModel],
        llms: Mapping[str, LLM],
        cfg: RealizeConfig,
        writer: _RowWriter,
        texts: dict[str, str],
        state: _State,
        sleep: Sleep,
    ) -> None:
        self.skeleton = skeleton
        self.models = models
        self.llms = llms
        self.cfg = cfg
        self.writer = writer
        self.texts = texts
        self.state = state
        self.sleep = sleep
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.recheck_reasons: dict[str, list[str]] = {}

    def _charge(
        self,
        model: FamilyModel,
        usage: LLMUsage,
        acc: _Attempts,
        reported: float | None = None,
    ) -> None:
        """호출 하나의 비용을 누적한다. 계산값(토큰 × 단가)과 보고값 중 큰 쪽을 쓴다."""
        computed = model.cost_usd(usage)
        cost = max(computed, reported) if reported is not None else computed
        acc.usage = acc.usage + usage
        acc.cost += cost
        acc.cost_computed += computed
        if reported is not None:
            acc.cost_reported = (acc.cost_reported or 0.0) + reported
        st = self.state
        st.spent += cost
        if st.stopped:
            return
        if st.spent >= self.cfg.max_cost_usd:
            st.stop_reason = "cost_cap"
        elif (
            self.cfg.max_total_usd is not None
            and st.prior_spent + st.spent >= self.cfg.max_total_usd
        ):
            st.stop_reason = "total_cap"

    def _priors(self, spec: BodySpec) -> dict[str, str]:
        out: dict[str, str] = {}
        for pid in spec.context.prior_body_ids:
            if pid in self.texts:
                out[pid] = self.texts[pid]
            elif pid in self.skeleton.bodies:
                out[pid] = self.skeleton.bodies[pid].template_text
                self.state.prior_template_used += 1
        return out

    async def _call(self, model: FamilyModel, request: LLMRequest, acc: _Attempts) -> LLMResponse:
        """일시 오류는 지수 백오프로 다시 시도한다(검증 실패 횟수와 따로 센다)."""
        llm = self.llms[model.family]
        for n in range(self.cfg.transient_retries + 1):
            if self.state.stopped:
                raise _Stopped
            self.state.requests += 1
            try:
                return await llm.complete(request)
            except TransientLLMError as exc:
                self._charge(model, exc.usage, acc, exc.cost_usd)
                if n >= self.cfg.transient_retries:
                    raise _Fatal(
                        f"{model.family}: 일시 오류가 계속된다({type(exc).__name__}, "
                        f"{self.cfg.transient_retries}회 재시도 후): {exc}"
                    ) from None
                acc.transient_retries += 1
                self.state.transient_retries += 1
                await self.sleep(self.cfg.transient_backoff_seconds * (2**n))
        raise _Fatal("도달할 수 없는 경로")  # pragma: no cover

    async def realize(self, body_id: str) -> None:
        spec = self.skeleton.bodies[body_id]
        model = self.models[spec.generator_family]
        acc = _Attempts()
        priors = self._priors(spec)
        hints: list[str] = []
        final_text: str | None = None
        for _ in range(self.cfg.max_attempts):
            if self.state.stopped:
                raise _Stopped
            acc.attempts += 1
            request = LLMRequest(
                messages=(
                    Message(role="system", content=build_system()),
                    Message(role="user", content=build_user(spec, priors, hints)),
                ),
                purpose="data_generation",
                max_output_tokens=model.max_output_tokens,
                temperature=model.temperature,
                metadata={"body_id": body_id, "prompt_version": PROMPT_VERSION},
            )
            reasons, new_hints, text = await self._attempt(model, spec, request, acc)
            if not reasons:
                final_text = text
                self.state.consecutive_mismatch[model.family] = 0
                self.state.consecutive_mismatch[f"zero:{model.family}"] = 0
                break
            acc.failures.append(reasons)
            hints = new_hints
        fallback = final_text is None
        text_out = spec.template_text if final_text is None else final_text
        self.texts[body_id] = text_out
        self.writer.write(
            {
                "body_id": body_id,
                "text": text_out,
                "family": model.family,
                "route": model.route,
                "model_id": acc.response_model or model.model_id,
                "prompt_version": PROMPT_VERSION,
                "attempts": acc.attempts,
                "fallback": fallback,
                "failure_reasons": acc.failures,
                "transient_retries": acc.transient_retries,
                **(
                    {"recheck_reasons": self.recheck_reasons[body_id]}
                    if body_id in self.recheck_reasons
                    else {}
                ),
                "input_tokens": acc.usage.input_tokens,
                "cached_input_tokens": acc.usage.cached_input_tokens,
                "output_tokens": acc.usage.output_tokens,
                "cost_usd": round(acc.cost, 8),
                "cost_computed_usd": round(acc.cost_computed, 8),
                "cost_reported_usd": (
                    None if acc.cost_reported is None else round(acc.cost_reported, 8)
                ),
            }
        )
        self.state.done_bodies += 1

    def _zero_usage(self, model: FamilyModel) -> tuple[list[str], list[str], str]:
        st = self.state
        st.zero_usage += 1
        if st.zero_usage == 1:
            print(
                f"경고: {model.family} 응답의 사용량이 0이다(zero_usage). 비용 과소 집계 가능",
                file=sys.stderr,
            )
        key = f"zero:{model.family}"
        n = st.consecutive_mismatch.get(key, 0) + 1
        st.consecutive_mismatch[key] = n
        if n >= self.cfg.mismatch_abort:
            raise _Fatal(f"{model.family}: 사용량 0 응답이 {n}번 연속이다. 응답 형식을 확인한다")
        return ["zero_usage"], [], ""

    async def _attempt(
        self, model: FamilyModel, spec: BodySpec, request: LLMRequest, acc: _Attempts
    ) -> tuple[list[str], list[str], str]:
        """시도 한 번. (실패 사유, 재시도 힌트, 본문)을 돌려준다. 사유가 비면 성공이다."""
        try:
            async with self.sem:
                response = await self._call(model, request, acc)
        except ModelMismatchError as exc:
            self._charge(model, exc.usage, acc, exc.cost_usd)
            acc.response_model = exc.response_model_id or acc.response_model
            n = self.state.consecutive_mismatch.get(model.family, 0) + 1
            self.state.consecutive_mismatch[model.family] = n
            if n >= self.cfg.mismatch_abort:
                raise _Fatal(
                    f"{model.family}: 응답 모델이 설정과 {n}번 연속 다르다"
                    f"(설정={model.model_id!r}, 응답={exc.response_model_id!r}). 모델 ID를 확인한다"
                ) from None
            return ["model_mismatch"], [], ""
        except MalformedResponseError as exc:
            self._charge(model, exc.usage, acc, exc.cost_usd)
            return ["malformed_response"], [], ""
        except LLMRequestError as exc:
            self._charge(model, exc.usage, acc, exc.cost_usd)
            raise _Fatal(f"{model.family}: 영구 오류({type(exc).__name__}): {exc}") from None
        except (_Fatal, _Stopped):
            raise
        except LLMError as exc:
            self._charge(model, exc.usage, acc, exc.cost_usd)
            raise _Fatal(f"{model.family}: LLM 오류({type(exc).__name__}): {exc}") from None
        self._charge(model, response.usage, acc, response.cost_usd)
        if response.usage.input_tokens == 0 and response.usage.output_tokens == 0:
            return self._zero_usage(model)
        if response.model_id:
            acc.response_model = response.model_id
        if response.finish_reason == "length":
            return ["truncated"], ["출력이 중간에 잘렸다. 목표 글자 수 안에서 끝낸다"], ""
        if response.finish_reason == "content_filter":
            return ["content_filter"], [], ""
        result = check_body(spec, response.text)
        return list(result.reasons), list(result.hints), result.text


class _Stopped(Exception):
    """비용 상한이나 영구 오류로 새 요청을 멈췄다. 이 본문은 기록하지 않는다."""


def _components(skeleton: Skeleton, ids: list[str]) -> list[list[str]]:
    """같은 스레드(앞선 메일 참조로 이어진 본문)끼리 묶고 스레드 순번 순으로 정렬한다."""
    pending = set(ids)
    parent = {b: b for b in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for b in ids:
        for p in skeleton.bodies[b].context.prior_body_ids:
            if p in pending:
                parent[find(b)] = find(p)
    groups: dict[str, list[str]] = defaultdict(list)
    for b in ids:
        groups[find(b)].append(b)
    return [
        sorted(g, key=lambda b: (skeleton.bodies[b].context.thread_position, b))
        for _, g in sorted(groups.items())
    ]


@dataclass
class RunResult:
    exit_code: int
    stop_reason: str | None
    fatal_message: str | None
    spent_usd: float
    requests: int
    generated: int
    skipped_done: int
    selected: int
    prior_spent_usd: float = 0.0
    notes: list[str] = field(default_factory=list)


async def realize_bodies(
    skeleton: Skeleton,
    out_dir: Path,
    models: Mapping[str, FamilyModel],
    cfg: RealizeConfig,
    *,
    llm_factory: LLMFactory | None = None,
    now: Now = lambda: datetime.now(UTC),
    sleep: Sleep = asyncio.sleep,
    skeleton_sha256: str = "",
    progress: Callable[[str], None] | None = None,
    targets: Sequence[str] | None = None,
    recheck_reasons: Mapping[str, list[str]] | None = None,
    run_extra: Mapping[str, Any] | None = None,
) -> RunResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = now()
    factory = llm_factory or build_family_llm
    manifest = load_manifest(out_dir)  # 깨졌으면 ManifestError(작업 전에 멈춘다)
    selected = select_ids(skeleton, cfg.families, cfg.limit, cfg.sample_seed)
    rows = read_rows(out_dir / BODIES_FILE)
    texts = {b: str(r["text"]) for b, r in rows.items() if b in skeleton.bodies}
    if targets is None:
        pending = [b for b in selected if b not in rows]
    else:  # --recheck: 검사에 실패한 본문만 다시 만든다(새 행이 이전 행을 덮는다)
        chosen = set(selected)
        pending = [b for b in targets if b in chosen]
    skipped = len(selected) - len(pending)

    state = _State()
    state.prior_spent = cumulative_spend(read_all_rows(out_dir / BODIES_FILE), manifest)
    if cfg.max_total_usd is not None and state.prior_spent >= cfg.max_total_usd:
        state.stop_reason = "total_cap"
    llms: dict[str, LLM] = {}
    if not state.stopped:
        for family in sorted({skeleton.bodies[b].generator_family for b in pending}):
            llms[family] = factory(models[family])

    writer = _RowWriter(out_dir / BODIES_FILE)
    realizer = _Realizer(skeleton, models, llms, cfg, writer, texts, state, sleep)
    realizer.recheck_reasons = dict(recheck_reasons or {})

    async def run_group(group: list[str]) -> None:
        for body_id in group:
            if state.stopped:
                return
            try:
                await realizer.realize(body_id)
            except _Stopped:
                return
            except _Fatal as exc:
                if not state.stopped or state.stop_reason == "cost_cap":
                    state.stop_reason = "fatal"
                    state.fatal_message = str(exc)
                return
            if progress is not None and state.done_bodies % 50 == 0:
                progress(f"진행 {state.done_bodies}/{len(pending)} 지출 ${state.spent:.4f}")

    finished = False
    try:
        tasks = [asyncio.ensure_future(run_group(g)) for g in _components(skeleton, pending)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # 하나가 비정상 종료하면 나머지도 멈춘다(gather는 형제 작업을 취소하지 않는다).
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finished = True
    finally:
        writer.close()
        # 중단·예외로 끝나도 지출을 남긴다(다음 실행의 누적 상한 계산에 쓴다).
        _write_manifest(
            out_dir,
            skeleton,
            skeleton_sha256,
            models,
            cfg,
            run_entry={
                "started_at": started.isoformat(),
                "finished_at": now().isoformat(),
                "selected": len(selected),
                "skipped_done": skipped,
                "generated": state.done_bodies,
                "requests": state.requests,
                "transient_retries": state.transient_retries,
                "zero_usage_responses": state.zero_usage,
                "cost_usd": round(state.spent, 8),
                "max_cost_usd": cfg.max_cost_usd,
                "max_total_usd": cfg.max_total_usd,
                "stop_reason": state.stop_reason if finished else "interrupted",
                "prior_template_used": state.prior_template_used,
                "limit": cfg.limit,
                "families": list(cfg.families),
                **(dict(run_extra) if run_extra else {}),
            },
        )

    exit_code = EXIT_OK
    if state.stop_reason == "fatal":
        exit_code = EXIT_FATAL
    elif state.stop_reason in ("cost_cap", "total_cap"):
        exit_code = EXIT_COST_CAP
    result = RunResult(
        exit_code=exit_code,
        stop_reason=state.stop_reason,
        fatal_message=state.fatal_message,
        spent_usd=state.spent,
        requests=state.requests,
        generated=state.done_bodies,
        skipped_done=skipped,
        selected=len(selected),
        prior_spent_usd=state.prior_spent,
    )
    if state.prior_template_used:
        result.notes.append(
            "앞선 메일 본문이 아직 없어 템플릿 본문을 맥락으로 쓴 횟수: "
            f"{state.prior_template_used}"
        )
    return result


# ---------------------------------------------------------------- 매니페스트·조립


def _length_distribution(
    skeleton: Skeleton, rows: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    by: dict[str, list[int]] = defaultdict(list)
    for bid, row in rows.items():
        spec = skeleton.bodies.get(bid)
        if spec is not None and not row.get("fallback"):
            by[spec.length_class].append(len(str(row["text"])))
    out: dict[str, Any] = {}
    for cls in ("short", "medium", "long"):
        v = sorted(by.get(cls, []))
        out[cls] = (
            {
                "n": len(v),
                "min": v[0],
                "median": statistics.median(v),
                "mean": round(statistics.fmean(v), 1),
                "max": v[-1],
            }
            if v
            else {"n": 0}
        )
    return out


def family_summary(
    skeleton: Skeleton,
    rows: Mapping[str, Mapping[str, Any]],
    all_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """계열별 집계. 본문 수·대체 수·사유는 최신 행, 시도·토큰·비용은 모든 행(재생성 포함)이다."""
    out: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        mine = [r for b, r in rows.items() if b in skeleton.bodies and r.get("family") == family]
        spent = (
            [r for r in all_rows if r.get("family") == family and r["body_id"] in skeleton.bodies]
            if all_rows is not None
            else mine
        )
        reasons = Counter(
            code for r in mine for attempt in r["failure_reasons"] for code in attempt
        )
        out[family] = {
            "generated": len(mine),
            "attempts": sum(int(r["attempts"]) for r in spent),
            "fallback": sum(1 for r in mine if r["fallback"]),
            "failure_reasons": dict(sorted(reasons.items())),
            "input_tokens": sum(int(r["input_tokens"]) for r in spent),
            "cached_input_tokens": sum(int(r["cached_input_tokens"]) for r in spent),
            "output_tokens": sum(int(r["output_tokens"]) for r in spent),
            "cost_usd": round(sum(float(r["cost_usd"]) for r in spent), 6),
            "total_in_skeleton": sum(
                1 for s in skeleton.bodies.values() if s.generator_family == family
            ),
        }
    return out


class ManifestError(Exception):
    """매니페스트 JSON이 깨졌거나 형식이 맞지 않는다."""


def load_manifest(out_dir: Path) -> dict[str, Any]:
    path = out_dir / MANIFEST_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ManifestError(f"{MANIFEST_FILE}을 읽을 수 없다(JSON이 깨졌다)") from None
    if not isinstance(data, dict):
        raise ManifestError(f"{MANIFEST_FILE}의 형식이 올바르지 않다")
    return data


def cumulative_spend(
    rows: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> float:
    """출력 디렉터리의 누적 지출. 본문 기록 합과 실행 기록 합 중 큰 쪽이다.

    비정상 종료로 실행 기록이 없으면 본문 기록 합이, 상한 때문에 버려진 요청의 지출은 실행 기록 합이
    더 정확하다.
    """
    values = rows.values() if isinstance(rows, Mapping) else rows
    from_rows = sum(float(r.get("cost_usd") or 0) for r in values)
    from_runs = sum(float(r.get("cost_usd") or 0) for r in manifest.get("runs", []))
    return max(from_rows, from_runs)


def bodies_sha256(skeleton: Skeleton) -> str:
    """skeleton의 `bodies` 구역(본문 명세 전체)의 해시. 명세가 같으면 본문을 재사용할 수 있다."""
    payload = {bid: spec.model_dump(mode="json") for bid, spec in skeleton.bodies.items()}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def differing_body_ids(old: Skeleton, new: Skeleton) -> list[str]:
    """본문 명세가 다르거나 한쪽에만 있는 body_id."""
    ids = set(old.bodies) | set(new.bodies)
    return sorted(b for b in ids if old.bodies.get(b) != new.bodies.get(b))


def _write_manifest(
    out_dir: Path,
    skeleton: Skeleton,
    skeleton_sha256: str,
    models: Mapping[str, FamilyModel],
    cfg: RealizeConfig,
    *,
    run_entry: Mapping[str, Any] | None = None,
    assembled: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = out_dir / MANIFEST_FILE
    previous = load_manifest(out_dir)
    all_rows = read_all_rows(out_dir / BODIES_FILE)
    rows = read_rows(out_dir / BODIES_FILE)
    runs: list[Any] = list(previous.get("runs", []))
    if run_entry is not None:
        runs.append(dict(run_entry))
    configs = {f: m.manifest() for f, m in models.items()}
    history: list[Any] = list(previous.get("previous_family_configs", []))
    old = previous.get("family_configs")
    if old and old != configs:
        history.append(old)
    skeleton_history: list[Any] = list(previous.get("previous_skeleton_sha256s", []))
    old_sha = previous.get("skeleton_sha256")
    if old_sha and old_sha != skeleton_sha256 and old_sha not in skeleton_history:
        skeleton_history.append(old_sha)
    manifest: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "skeleton_schema_version": skeleton.schema_version,
        "skeleton_sha256": skeleton_sha256,
        "previous_skeleton_sha256s": skeleton_history,
        "bodies_sha256": bodies_sha256(skeleton),
        "skeleton_seed": skeleton.seed,
        "sample_seed": cfg.sample_seed,
        "family_configs": configs,
        "previous_family_configs": history,
        "families": family_summary(skeleton, rows, all_rows),
        "prompt_versions_in_rows": dict(
            sorted(Counter(str(r.get("prompt_version")) for r in rows.values()).items())
        ),
        "length_chars_by_class": _length_distribution(skeleton, rows),
        "total_bodies_in_skeleton": len(skeleton.bodies),
        "bodies_done": len([b for b in rows if b in skeleton.bodies]),
        "total_cost_usd": round(cumulative_spend(all_rows, {"runs": runs}), 6),
        "runs": runs,
        "assembled": dict(assembled) if assembled is not None else previous.get("assembled"),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_json(manifest), encoding="utf-8")
    os.replace(tmp, path)  # 쓰다 끊겨도 기존 매니페스트가 깨지지 않는다
    return manifest


def assemble(
    skeleton: Skeleton,
    out_dir: Path,
    models: Mapping[str, FamilyModel],
    cfg: RealizeConfig,
    *,
    allow_template_missing: bool,
    skeleton_sha256: str = "",
    skeleton_change: Mapping[str, Any] | None = None,
) -> int:
    """`world.json`을 만든다. 본문이 빠진 body_id가 있으면 `allow_template_missing`일 때만 한다.

    `skeleton_change`가 있으면(이전 skeleton과 bodies 구역이 같은 재조립) 매니페스트의 `assembled`에
    그대로 기록한다.
    """
    rows = read_rows(out_dir / BODIES_FILE)
    texts = {b: str(r["text"]) for b, r in rows.items() if b in skeleton.bodies}
    missing = sorted(set(skeleton.bodies) - set(texts))
    if missing and not allow_template_missing:
        print(
            f"조립 불가: 본문이 없는 body_id {len(missing)}개 "
            "(--allow-template-missing이면 템플릿으로 채운다)",
            file=sys.stderr,
        )
        return EXIT_CANNOT_ASSEMBLE
    fallback = sum(1 for b in texts if rows[b].get("fallback"))
    data = export_world(skeleton, texts)
    SimWorld.load(data, FixedClock(skeleton.snapshot_time))  # 로드되지 않으면 예외
    (out_dir / WORLD_FILE).write_text(dump_json(data), encoding="utf-8")
    _write_manifest(
        out_dir,
        skeleton,
        skeleton_sha256,
        models,
        cfg,
        assembled={
            "world_file": WORLD_FILE,
            "template_substitutions": fallback + len(missing),
            "fallback": fallback,
            "missing_filled_with_template": len(missing),
            **({"skeleton_change": dict(skeleton_change)} if skeleton_change else {}),
        },
    )
    print(
        f"world.json 조립 완료: 본문 {len(skeleton.bodies)}개 "
        f"(템플릿 대체 {fallback + len(missing)}개) -> {out_dir / WORLD_FILE}"
    )
    return EXIT_OK


# ---------------------------------------------------------------- 비용 추정


def estimate(
    skeleton: Skeleton,
    ids: list[str],
    models: Mapping[str, FamilyModel],
    *,
    tokens_per_char: float,
    attempt_factor: float,
    reasoning_tokens: int,
) -> dict[str, dict[str, Any]]:
    """호출 없이 계열별 예상 토큰·비용을 계산한다.

    가정(모두 측정하지 않은 값이다): 한국어 1글자 = `tokens_per_char` 토큰(입력·출력 공통),
    출력 글자 수 = 목표 범위의 중간값, 시도 수 = 본문당 `attempt_factor`회(다시 쓰는 비용 포함),
    추론 강도를 보내는 경로는 요청마다 `reasoning_tokens`를 출력에 더한다, 캐시 적중 없음.
    입력은 시스템 프롬프트와 실제 사용자 메시지(앞선 메일은 템플릿 본문으로 대신한다)의 글자 수다.
    """
    sys_chars = len(build_system())
    per: dict[str, dict[str, float]] = defaultdict(
        lambda: {"bodies": 0.0, "input_tokens": 0.0, "output_tokens": 0.0}
    )
    for bid in ids:
        spec = skeleton.bodies[bid]
        priors = {
            p: skeleton.bodies[p].template_text
            for p in spec.context.prior_body_ids
            if p in skeleton.bodies
        }
        in_chars = sys_chars + len(build_user(spec, priors))
        out_chars = (spec.target_chars[0] + spec.target_chars[1]) / 2
        model = models[spec.generator_family]
        extra = reasoning_tokens if model.route == "openai" and model.reasoning_effort else 0
        d = per[spec.generator_family]
        d["bodies"] += 1
        d["input_tokens"] += in_chars * tokens_per_char * attempt_factor
        d["output_tokens"] += (out_chars * tokens_per_char + extra) * attempt_factor
    out: dict[str, dict[str, Any]] = {}
    for family, d in sorted(per.items()):
        m = models[family]
        usage = LLMUsage(input_tokens=int(d["input_tokens"]), output_tokens=int(d["output_tokens"]))
        out[family] = {
            "bodies": int(d["bodies"]),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": round(m.cost_usd(usage), 4),
            "route": m.route,
            "model_id": m.model_id,
        }
    return out


# ---------------------------------------------------------------- CLI


def cap_message(reason: str, cfg: RealizeConfig) -> str:
    if reason == "total_cap":
        return (
            f"누적 지출 상한 ${cfg.max_total_usd}에 닿아 멈췄다. "
            "상한을 올리면 같은 명령으로 이어서 실행할 수 있다"
        )
    return f"이번 실행 비용 상한 ${cfg.max_cost_usd}에 닿아 멈췄다. 같은 명령으로 이어서 실행한다"


def _load_skeleton(path: str) -> tuple[Skeleton, str]:
    p = Path(path)
    if p.is_dir():
        p = p / "skeleton.json"
    raw = p.read_bytes()
    return Skeleton.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def recheck_scan(
    skeleton: Skeleton, rows: Mapping[str, Mapping[str, Any]], selected: set[str]
) -> tuple[int, dict[str, list[str]], int]:
    """최신 행을 현재 검사 규칙으로 다시 본다. (검사한 수, {실패 body_id: 사유}, 버전 없는 행 수).

    템플릿으로 대체된 행(fallback)은 LLM이 쓴 본문이 아니라 검사하지 않는다.
    """
    checked = 0
    failed: dict[str, list[str]] = {}
    no_version = 0
    for bid in sorted(selected):
        row = rows.get(bid)
        spec = skeleton.bodies.get(bid)
        if row is None or spec is None or row.get("fallback"):
            continue
        if not row.get("prompt_version"):
            no_version += 1
        checked += 1
        result = check_body(spec, str(row.get("text", "")))
        if not result.ok:
            failed[bid] = list(result.reasons)
    return checked, failed, no_version


def check_skeleton_change(
    out_dir: Path, skeleton: Skeleton, skeleton_sha: str, previous_path: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """`--allow-skeleton-change` 검사. (기록할 변경 정보, 오류 메시지)를 돌려준다.

    매니페스트의 skeleton이 지금과 같으면 변경 정보는 `None`이다. 다르면 이전 `bodies` 구역과
    지금의 것이 같을 때만 허용한다.
    """
    try:
        prev = load_manifest(out_dir)
    except ManifestError as exc:
        return None, str(exc)
    prev_sha = prev.get("skeleton_sha256")
    if not prev_sha or prev_sha == skeleton_sha:
        return None, None
    new_bodies = bodies_sha256(skeleton)
    old_bodies = prev.get("bodies_sha256")
    old_skeleton: Skeleton | None = None
    if previous_path:
        try:
            old_skeleton, old_sha = _load_skeleton(previous_path)
        except (OSError, ValidationError, ValueError) as exc:
            return None, f"--previous-skeleton을 읽을 수 없다: {type(exc).__name__}"
        if old_sha != prev_sha:
            return None, "--previous-skeleton의 sha가 매니페스트의 skeleton_sha256과 다르다"
        old_bodies = bodies_sha256(old_skeleton)
    if not old_bodies:
        return None, (
            "이전 매니페스트에 bodies_sha256이 없다. --previous-skeleton으로 이전 skeleton을 준다"
        )
    if old_bodies != new_bodies:
        detail = ""
        if old_skeleton is not None:
            diff = differing_body_ids(old_skeleton, skeleton)
            detail = f" 다른 body_id {len(diff)}개(전체 {len(skeleton.bodies)}개)."
        else:
            detail = " 다른 body_id 수는 --previous-skeleton을 주면 센다."
        return None, "본문 명세(bodies 구역)가 이전 skeleton과 다르다. 재조립하지 않는다." + detail
    return {
        "previous_skeleton_sha256": prev_sha,
        "skeleton_sha256": skeleton_sha,
        "bodies_sha256": new_bodies,
        "previous_bodies_sha256": old_bodies,
    }, None


def _check_resume(
    out_dir: Path,
    skeleton_sha: str,
    *,
    allow_prompt_change: bool = False,
    allow_skeleton_change: bool = False,
) -> str | None:
    manifest = out_dir / MANIFEST_FILE
    if not manifest.exists():
        return None
    try:
        prev = load_manifest(out_dir)
    except ManifestError as exc:
        return str(exc)
    if (
        prev.get("skeleton_sha256")
        and prev["skeleton_sha256"] != skeleton_sha
        and not allow_skeleton_change
    ):
        return "출력 디렉터리의 매니페스트가 다른 skeleton으로 만든 것이다. 새 디렉터리를 쓴다"
    if (
        not allow_prompt_change
        and prev.get("prompt_version")
        and prev["prompt_version"] != PROMPT_VERSION
    ):
        return "출력 디렉터리의 본문이 다른 프롬프트 버전으로 만든 것이다. 새 디렉터리를 쓴다"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orbit Sim 세계 v1 본문 실현(LLM)")
    parser.add_argument("--skeleton", required=True, help="skeleton.json 또는 그 디렉터리")
    parser.add_argument("--out", required=True, help="출력 디렉터리(저장소 밖 임시 위치 권장)")
    parser.add_argument("--max-cost-usd", type=float, required=True, help="이번 실행의 비용 상한")
    parser.add_argument(
        "--max-total-usd",
        type=float,
        default=None,
        help="출력 디렉터리 전체 누적 지출 상한(이전 실행 포함, 선택)",
    )
    parser.add_argument("--families", default=",".join(FAMILIES), help="쉼표로 구분한 계열")
    parser.add_argument("--limit", type=int, default=None, help="표본 총 건수(계열별로 균등 분할)")
    parser.add_argument("--sample-seed", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--models-config", default=None, help="계열별 모델 설정 TOML")
    parser.add_argument("--openai-route", choices=("openai", "openrouter"), default=None)
    parser.add_argument("--dry-run", action="store_true", help="비용만 추정하고 호출하지 않는다")
    parser.add_argument("--assemble", action="store_true", help="world.json을 조립한다")
    parser.add_argument("--allow-template-missing", action="store_true")
    parser.add_argument(
        "--allow-skeleton-change",
        action="store_true",
        help="--assemble과 함께: bodies 구역이 같으면 skeleton이 바뀌어도 기존 본문으로 재조립한다",
    )
    parser.add_argument(
        "--previous-skeleton",
        default=None,
        help="이전 skeleton.json(매니페스트에 bodies_sha256이 없거나 다른 body_id 수를 셀 때)",
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="기존 본문을 현재 검사 규칙으로 다시 검사하고 실패한 것만 다시 생성한다",
    )
    parser.add_argument("--tokens-per-char", type=float, default=1.0, help="추정 가정")
    parser.add_argument("--attempt-factor", type=float, default=1.3, help="추정 가정(본문당 시도)")
    parser.add_argument("--reasoning-tokens", type=int, default=300, help="추정 가정(openai)")
    args = parser.parse_args(argv)

    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    if not families or any(f not in FAMILIES for f in families):
        print(f"--families는 {', '.join(FAMILIES)} 중에서 고른다", file=sys.stderr)
        return EXIT_USAGE
    if (
        args.max_cost_usd <= 0
        or (args.max_total_usd is not None and args.max_total_usd <= 0)
        or args.concurrency < 1
        or (args.limit is not None and args.limit < 1)
    ):
        print(
            "--max-cost-usd·--max-total-usd·--concurrency·--limit 값이 올바르지 않다",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        skeleton, sha = _load_skeleton(args.skeleton)
        models = load_models(args.models_config, openai_route=args.openai_route)
    except ModelConfigError as exc:
        print(f"모델 설정 오류: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValidationError, ValueError) as exc:
        print(f"입력 오류: {type(exc).__name__}", file=sys.stderr)
        return EXIT_USAGE
    if skeleton.schema_version != SCHEMA_VERSION:
        print("skeleton 스키마 버전이 맞지 않는다", file=sys.stderr)
        return EXIT_USAGE

    cfg = RealizeConfig(
        max_cost_usd=args.max_cost_usd,
        max_total_usd=args.max_total_usd,
        concurrency=args.concurrency,
        families=families,
        limit=args.limit,
        sample_seed=args.sample_seed,
    )
    out_dir = Path(args.out)
    selected = select_ids(skeleton, cfg.families, cfg.limit, cfg.sample_seed)
    rows = read_rows(out_dir / BODIES_FILE)
    try:
        prior_spent = cumulative_spend(rows, load_manifest(out_dir))
    except ManifestError as exc:
        print(f"{exc}. 출력 디렉터리를 확인한다", file=sys.stderr)
        return EXIT_USAGE
    pending = [b for b in selected if b not in rows]
    recheck_failed: dict[str, list[str]] = {}
    recheck_extra: dict[str, Any] | None = None
    if args.recheck:
        checked, recheck_failed, no_version = recheck_scan(skeleton, rows, set(selected))
        by_reason = Counter(r for rs in recheck_failed.values() for r in rs)
        pending = sorted(recheck_failed)
        print(
            f"재검사: {checked}건 중 실패 {len(recheck_failed)}건, "
            f"사유 {dict(sorted(by_reason.items()))}"
        )
        if no_version:
            print(f"경고: prompt_version이 없는 행 {no_version}건", file=sys.stderr)
        recheck_extra = {
            "recheck": {
                "checked": checked,
                "failed": len(recheck_failed),
                "failure_reasons": dict(sorted(by_reason.items())),
                "rows_without_prompt_version": no_version,
                "prompt_version": PROMPT_VERSION,
            }
        }
    est = estimate(
        skeleton,
        pending,
        models,
        tokens_per_char=args.tokens_per_char,
        attempt_factor=args.attempt_factor,
        reasoning_tokens=args.reasoning_tokens,
    )
    total = sum(e["cost_usd"] for e in est.values())
    print(
        f"대상 {len(selected)}개 중 남은 {len(pending)}개. 가정: 1글자={args.tokens_per_char}토큰, "
        f"본문당 시도 {args.attempt_factor}회, openai 추론 {args.reasoning_tokens}토큰/요청, "
        "캐시 없음"
    )
    for family, e in est.items():
        print(
            f"  {family}: {e['bodies']}개, 입력 {e['input_tokens']:,} / "
            f"출력 {e['output_tokens']:,} "
            f"토큰, 예상 ${e['cost_usd']:.4f} ({e['route']}, {e['model_id']})"
        )
    total_cap = f", 누적 상한 ${args.max_total_usd}" if args.max_total_usd is not None else ""
    print(f"예상 합계 ${total:.4f} (이번 실행 상한 ${args.max_cost_usd}{total_cap})")
    print(f"이전 실행까지 누적 지출 ${prior_spent:.4f}")
    if args.dry_run:
        return EXIT_OK

    skeleton_change: dict[str, Any] | None = None
    if args.allow_skeleton_change:
        if not args.assemble or args.recheck:
            print(
                "--allow-skeleton-change는 --assemble에서만 쓴다(--recheck 불가)", file=sys.stderr
            )
            return EXIT_USAGE
        if pending:
            print(
                f"본문이 없는 body_id {len(pending)}개가 있어 LLM 호출이 필요하다. "
                "--allow-skeleton-change는 호출 없이 재조립할 때만 쓴다",
                file=sys.stderr,
            )
            return EXIT_USAGE
        skeleton_change, change_problem = check_skeleton_change(
            out_dir, skeleton, sha, args.previous_skeleton
        )
        if change_problem:
            print(change_problem, file=sys.stderr)
            return EXIT_USAGE
    problem = _check_resume(
        out_dir,
        sha,
        allow_prompt_change=args.recheck,
        allow_skeleton_change=args.allow_skeleton_change,
    )
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_USAGE

    code = EXIT_OK
    if pending or args.recheck:
        try:
            result = asyncio.run(
                realize_bodies(
                    skeleton,
                    out_dir,
                    models,
                    cfg,
                    skeleton_sha256=sha,
                    progress=lambda m: print(m, flush=True),
                    targets=pending if args.recheck else None,
                    recheck_reasons=recheck_failed,
                    run_extra=recheck_extra,
                )
            )
        except ManifestError as exc:
            print(f"{exc}. 출력 디렉터리를 확인한다", file=sys.stderr)
            return EXIT_USAGE
        except ValidationError as exc:
            # 오류 항목의 입력값에 키가 들어 있을 수 있어 필드 이름만 출력한다.
            names = sorted(
                {".".join(str(p) for p in e["loc"]) or "(설정 전체)" for e in exc.errors()}
            )
            print(
                f"LLM 설정 오류 (확인할 항목: {', '.join(names)}). 호출 경로에 맞는 "
                "ORBIT_OPENAI_API_KEY 또는 ORBIT_OPENROUTER_API_KEY가 있는지 확인한다",
                file=sys.stderr,
            )
            return EXIT_USAGE
        code = result.exit_code
        print(
            f"생성 {result.generated}건, 요청 {result.requests}회, 이번 실행 지출 "
            f"${result.spent_usd:.4f}, 누적 지출 ${result.prior_spent_usd + result.spent_usd:.4f}, "
            f"이미 있던 {result.skipped_done}건 건너뜀"
        )
        for note in result.notes:
            print(f"참고: {note}")
        if result.stop_reason in ("cost_cap", "total_cap"):
            print(cap_message(result.stop_reason, cfg))
        elif result.stop_reason == "fatal":
            print(f"영구 오류로 멈췄다: {result.fatal_message}", file=sys.stderr)
            return code
    else:
        print("남은 본문이 없다")

    if args.assemble:
        acode = assemble(
            skeleton,
            out_dir,
            models,
            cfg,
            allow_template_missing=args.allow_template_missing,
            skeleton_sha256=sha,
            skeleton_change=skeleton_change,
        )
        if acode != EXIT_OK:
            if code == EXIT_COST_CAP:  # 상한에 닿아 멈춘 사실이 조립 실패보다 먼저다
                print(cap_message("cost_cap", cfg), file=sys.stderr)
                return code
            return acode
    return code


if __name__ == "__main__":
    raise SystemExit(main())
