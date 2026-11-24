"""Agent 최종 채점: 코드 구조 검사 + Jev v4 + Gemini 2차 판정 (ADR-0040).

결과 JSONL(코드 채점)을 읽어 `<이름>.graded.jsonl`을 새로 쓴다. 원래 결과 파일은 고치지 않는다.
판정 근거(Jev 확률, Gemini 판정)는 `<이름>.grade/`의 `jev.jsonl`, `correctness.jsonl`에 쌓고
다시 채점할 때 재사용한다. 규칙은 ADR-0040 §2 참고.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from orbit.evaldata.v1.auto_review import ReviewConfig, latest_rows, read_rows, total_cost
from orbit.evaldata.v1.realize_models import FamilyModel, LLMFactory
from orbit.evalrunner.judge import judge_models
from orbit.evalrunner.rag_arch import jev
from orbit.evalrunner.rag_arch.correctness import (
    JUDGED_FILE,
    PROMPT_SHA,
    Episode,
    Fact,
    Task,
    estimate_cost,
    run_correctness,
    usable_row,
)
from orbit.evalrunner.runner import SealedSplitError
from orbit.evalrunner.task_source import TaskView

RULE_VERSION = "agent-final-v1"  # 판정 규칙(대상 선정·코드 문자열 판정·제목 붙이기)을 바꾸면 올린다
GEMINI_CONFIG = jev.REPO_ROOT / "eval/graders/gemini-correctness-v2.toml"
JEV_THRESHOLD = 0.25
BAND_LOW, BAND_HIGH = 0.1, 0.6  # Jev 확률 최솟값이 [LOW, HIGH)이면 Gemini가 최종 판정
KEYS = (jev.KEY_ENV, "ORBIT_OPENROUTER_API_KEY")
TEXT_CHECKS = frozenset({"answer_facts", "must_not_contain"})
KIND_CHECKS = TEXT_CHECKS | {"structured_answer"}
DUP_TIME_DESC, DUP_TITLE_DESC = "일정 시작 시각", "일정 제목"


class GradeError(ValueError):
    """채점 입력 오류. 메시지는 이름·경로만 담는다."""


@dataclass(frozen=True)
class GradeTask:
    task_id: str
    question: str
    facts: tuple[Fact, ...]  # Jev 입력: 설명이 겹치는 과제에만 제목을 붙인다
    gemini_facts: tuple[Fact, ...]  # Gemini 입력: 겹침과 관계없이 제목을 붙인다
    expected_kind: str | None
    expected_event: Mapping[str, Any] | None
    split: str | None = None  # 데이터셋 과제의 분할. 봉인 검사에 쓴다


def question_of(turns: Sequence[str]) -> str:
    if len(turns) == 1:
        return turns[0]
    return "\n".join(f"{i + 1}번째 요청: {u}" for i, u in enumerate(turns))


def context_facts(facts: Sequence[Mapping[str, Any]], *, always: bool = False) -> tuple[Fact, ...]:
    """시작 시각 설명 앞에 바로 앞 일정 제목을 붙인다. `always`가 아니면 설명이 겹칠 때만."""
    dup = always or len({f["description"] for f in facts}) < len(facts)
    out: list[Fact] = []
    title: str | None = None
    for f in facts:
        desc = f["description"]
        if dup and desc == DUP_TITLE_DESC:
            title = f["any_of"][0]
        elif dup and desc == DUP_TIME_DESC and title:
            desc = f"'{title}' 일정의 시작 시각"
        out.append(Fact(f["fact_id"], desc, tuple(f["any_of"])))
    return tuple(out)


def grade_tasks(tasks: Iterable[TaskView]) -> dict[str, GradeTask]:
    out: dict[str, GradeTask] = {}
    for t in tasks:
        ea = t.gold.expected_answer
        event = getattr(ea, "event", None)
        raw = [f.model_dump() for f in t.gold.answer_facts]
        out[t.task_id] = GradeTask(
            t.task_id,
            question_of([x.content for x in t.turns if x.role == "user"]),
            context_facts(raw),
            context_facts(raw, always=True),
            ea.kind if ea is not None else None,
            event.model_dump() if event is not None else None,
            getattr(t, "split", None),
        )
    return out


def fingerprint(model: FamilyModel) -> str:
    spec = {
        "rule_version": RULE_VERSION,
        "jev_model": jev.JEV_MODEL,
        "jev_prompt": jev.V4,
        "jev_threshold": JEV_THRESHOLD,
        "gemini_model": model.model_id,
        "gemini_prompt_sha": PROMPT_SHA,
        "band": [BAND_LOW, BAND_HIGH],
    }
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- 대상 선정(순수)


@dataclass(frozen=True)
class Entry:
    row: Mapping[str, Any]
    task: Task | None  # Jev 입력. 사실이 없는 과제는 None
    gemini_task: Task | None
    episode: Episode | None
    target: bool  # Jev 적용 대상
    empty: bool  # 답 문장이 없다


def code_text_verdict(row: Mapping[str, Any]) -> bool:
    """코드 문자열 판정: 사실 재현율 1이고 must_not_contain이 실패하지 않았다."""
    checks = row.get("checks") or {}
    return row.get("text_fact_recall") == 1.0 and checks.get("must_not_contain") is not False


def is_target(row: Mapping[str, Any], gt: GradeTask) -> bool:
    if not gt.facts:
        return False
    out = row.get("outcome") or {}
    bad = {c for c, v in (row.get("checks") or {}).items() if v is False}
    if out.get("success") is True:
        return True
    if bad and bad <= TEXT_CHECKS:
        return True
    ids = row.get("answer_ids") or {}
    return bool(  # C02: 일정은 맞고 구조화 답의 종류 표시만 다르다. event 대 event_set은 실패
        out.get("failure_type") == "answer_kind"
        and gt.expected_kind == "event_time"
        and ids.get("kind") == "event"
        and ids.get("event") == gt.expected_event
        and bad <= KIND_CHECKS
    )


def plan(
    rows: Sequence[Mapping[str, Any]], tasks: Mapping[str, GradeTask], arch: str
) -> list[Entry]:
    entries: list[Entry] = []
    seen: set[str] = set()
    for row in rows:
        gt = tasks.get(str(row.get("task_id")))
        if gt is None:
            raise GradeError(f"데이터셋에 없는 과제: {row.get('task_id')}")
        if not gt.facts:
            entries.append(Entry(row, None, None, None, False, False))
            continue
        text = (row.get("trace") or {}).get("final_text")
        split = (row.get("config") or {}).get("split")
        try:
            repeat = int(row["repeat"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GradeError(f"repeat가 없거나 정수가 아니다: {gt.task_id}") from exc
        ep = Episode(arch, gt.task_id, repeat, str(split), text or "")
        if ep.item_id in seen:
            raise GradeError(f"episode가 겹친다: {ep.item_id}")
        seen.add(ep.item_id)
        entries.append(
            Entry(
                row,
                Task(gt.task_id, "agent", gt.question, gt.facts),
                Task(gt.task_id, "agent", gt.question, gt.gemini_facts),
                ep,
                is_target(row, gt),
                not ep.answer.strip(),
            )
        )
    return entries


def jev_items(entries: Sequence[Entry]) -> list[tuple[Task, Episode]]:
    return [
        (e.task, e.episode) for e in entries if e.target and not e.empty and e.task and e.episode
    ]


def needs_gemini(code_text: bool, min_prob: float) -> bool:
    return code_text != (min_prob >= JEV_THRESHOLD) or BAND_LOW <= min_prob < BAND_HIGH


def usable_jev(row: Mapping[str, Any] | None, task: Task, ep: Episode) -> bool:
    return (
        row is not None
        and row.get("status") == "ok"
        and bool(row.get("probs"))
        and row.get("input_sha") == jev.body_sha(jev.build_body(task, ep, jev.V4))
    )


def jev_by_item(
    rows: Sequence[Mapping[str, Any]], entries: Sequence[Entry]
) -> dict[str, Mapping[str, Any]]:
    ok = {
        str(r["item_id"]): r
        for r in rows
        if r.get("prompt_version") == jev.V4 and r.get("status") == "ok"
    }
    return {
        e.episode.item_id: ok[e.episode.item_id]
        for e in entries
        if e.target
        and e.task
        and e.episode
        and usable_jev(ok.get(e.episode.item_id), e.task, e.episode)
    }


def gemini_entries(entries: Sequence[Entry], jevs: Mapping[str, Mapping[str, Any]]) -> list[Entry]:
    return [
        e
        for e in entries
        if e.episode is not None
        and e.episode.item_id in jevs
        and needs_gemini(code_text_verdict(e.row), jev.item_prob(jevs[e.episode.item_id]))
    ]


def gemini_by_item(
    rows: Sequence[Mapping[str, Any]], entries: Sequence[Entry], family: str
) -> dict[str, bool]:
    latest = latest_rows(rows)
    out: dict[str, bool] = {}
    for e in entries:
        if e.gemini_task is None or e.episode is None:
            continue
        r = latest.get((e.episode.item_id, family))
        if usable_row(r, e.gemini_task, e.episode):
            assert r is not None  # noqa: S101  usable_row가 보장한다
            out[e.episode.item_id] = bool(r["correct"])
    return out


def final_row(
    e: Entry,
    jevs: Mapping[str, Mapping[str, Any]],
    gemini: Mapping[str, bool],
    fp: str,
) -> dict[str, Any]:
    """결과 행에 최종 판정과 `grade` 필드를 붙인 새 행. 원문(답 문장)은 grade에 넣지 않는다."""
    row = dict(e.row)
    out = dict(row.get("outcome") or {})
    grade: dict[str, Any] = {
        "rule": "code",
        "code_success": out.get("success"),
        "code_failure_type": out.get("failure_type"),
        "jev_min_prob": None,
        "gemini_correct": None,
        "grader_fingerprint": fp,
    }
    if e.target and e.episode is not None:
        item = e.episode.item_id
        jrow = jevs.get(item)
        prob = jev.item_prob(jrow) if jrow is not None else None
        use_gemini = prob is not None and needs_gemini(code_text_verdict(e.row), prob)
        grade["rule"] = "gemini" if use_gemini else "jev"
        grade["jev_min_prob"] = prob
        grade["gemini_correct"] = gemini.get(item)
        verdict: bool | None
        if e.empty or prob is None:
            verdict = None
        elif use_gemini:
            verdict = gemini.get(item)
        else:
            verdict = prob >= JEV_THRESHOLD
        kind = "answer_kind" if grade["code_failure_type"] == "answer_kind" else "wrong_answer"
        out = {
            "success": verdict,
            "failure_type": None if verdict is not False else kind,
            "detail": out.get("detail") if verdict == out.get("success") else None,
        }
        row["outcome"] = out
    row["grade"] = grade
    return row


def finalize(
    entries: Sequence[Entry],
    jevs: Mapping[str, Mapping[str, Any]],
    gemini: Mapping[str, bool],
    fp: str,
) -> list[dict[str, Any]]:
    return [final_row(e, jevs, gemini, fp) for e in entries]


# ---------------------------------------------------------------- 실행


def graded_path(results: Path) -> Path:
    return results.with_name(results.stem + ".graded.jsonl")


def grade_dir(results: Path) -> Path:
    return results.with_name(results.stem + ".grade")


def missing_keys() -> list[str]:
    return [k for k in KEYS if not os.environ.get(k)]


def read_results(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GradeError(f"결과 파일을 읽을 수 없다: {type(exc).__name__}") from exc
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            raise GradeError(f"{n}번째 줄이 JSON이 아니다") from None
        if not isinstance(row, dict) or "task_id" not in row or "outcome" not in row:
            raise GradeError(f"{n}번째 줄이 결과 형식이 아니다")
        rows.append(row)
    return rows


@dataclass
class GradeReport:
    rows: int = 0
    targets: int = 0
    jev_pending: int = 0
    jev_est_usd: float = 0.0
    gemini_targets: int | None = None  # Jev 판정이 모두 있을 때만 안다
    gemini_pending: int = 0
    gemini_est_usd: float = 0.0
    spent_usd: float = 0.0
    undecided: int = 0  # 판정이 null인 에피소드
    stop_reason: str | None = None  # 상한·오류로 판정이 빠졌다
    written: Path | None = None
    notes: list[str] = field(default_factory=list)


def _jev_history(path: Path) -> list[dict[str, Any]]:
    return jev._read_jsonl(path)


def _estimate_jev(
    items: Sequence[tuple[Task, Episode]], history: Sequence[Mapping[str, Any]]
) -> tuple[int, float]:
    pending = pending_jev(items, history)
    return len(pending), sum(
        jev.estimate_request_cost(jev.build_body(t, e, jev.V4)) for t, e in pending
    )


def pending_jev(
    items: Sequence[tuple[Task, Episode]], history: Sequence[Mapping[str, Any]]
) -> list[tuple[Task, Episode]]:
    """input_sha까지 맞는 판정이 없는 항목. 오래된 판정은 쓰지 않으므로 남은 호출로 센다."""
    ok = {
        str(r["item_id"]): r
        for r in history
        if r.get("prompt_version") == jev.V4 and r.get("status") == "ok"
    }
    return [(t, e) for t, e in items if not usable_jev(ok.get(e.item_id), t, e)]


async def grade_results(
    results: Path,
    tasks: Mapping[str, GradeTask],
    *,
    max_cost_usd: float | None,
    dry_run: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    llm_factory: LLMFactory | None = None,
    progress: Any = None,
    allow_test: bool = False,
) -> GradeReport:
    rows = read_results(results)
    if (
        not dry_run
        and not allow_test
        and (
            any((r.get("config") or {}).get("split") == "test" for r in rows)
            or any(
                (gt := tasks.get(str(r.get("task_id")))) is not None and gt.split == "test"
                for r in rows
            )
        )
    ):
        raise SealedSplitError("test 분할 봉인")  # 정답 사실이 외부 Judge로 나가기 전에 거부한다
    entries = plan(rows, tasks, results.stem)
    out_dir = grade_dir(results)
    model = judge_models(str(GEMINI_CONFIG), ["google"])["google"]
    fp = fingerprint(model)
    items = jev_items(entries)
    report = GradeReport(rows=len(rows), targets=sum(e.target for e in entries))
    jfile = out_dir / jev.JEV_FILE
    jhist = _jev_history(jfile)
    report.jev_pending, report.jev_est_usd = _estimate_jev(items, jhist)
    jprior = jev.prior_cost(jhist)
    jevs = jev_by_item(jhist, entries)
    ghist = read_rows(out_dir / JUDGED_FILE)
    stale = sum(
        1
        for t, e in pending_jev(items, jhist)
        if any(
            r.get("item_id") == e.item_id
            and r.get("prompt_version") == jev.V4
            and r.get("status") == "ok"
            for r in jhist
        )
    )

    def gem_stage() -> tuple[list[Entry], list[tuple[Task, Episode]]]:
        g = gemini_entries(entries, jevs)
        return g, [(e.gemini_task, e.episode) for e in g if e.gemini_task and e.episode]

    if dry_run:
        if report.jev_pending == 0:
            g, gitems = gem_stage()
            report.gemini_targets = len(g)
            est = estimate_cost(gitems, model, ghist)
            report.gemini_pending, report.gemini_est_usd = est["calls"], est["cost_usd"]
        else:
            report.notes.append("Jev 판정이 없는 건이 있어 Gemini 대상 수는 Jev 실행 뒤에 정해진다")
        return report
    if max_cost_usd is None or max_cost_usd <= 0:
        raise GradeError("--max-cost-usd(채점 비용 상한)가 필요하다")
    if stale:  # 입력이 바뀐 옛 판정은 재사용할 수 없고 jev가 다시 부르지도 않는다
        raise GradeError(
            f"{jfile}에 현재 입력과 맞지 않는 이전 Jev 판정이 {stale}건 있다. "
            "그 파일을 지우거나 옮긴 뒤 다시 실행하라"
        )
    gprior = total_cost(ghist)
    if report.jev_pending and jprior + gprior + report.jev_est_usd > max_cost_usd:
        report.stop_reason = "cost_cap"
        report.notes.append("Jev 추정 비용이 상한을 넘어 호출하지 않았다")
        return report
    if report.jev_pending:
        out_dir.mkdir(parents=True, exist_ok=True)
        s = await jev.run_jev(
            items, jfile, api_key=os.environ.get(jev.KEY_ENV, ""),
            max_cost_usd=max_cost_usd - gprior,
            prompt_version=jev.V4, transport=transport, progress=progress,
        )  # fmt: skip
        report.spent_usd += s.spent_usd
        if s.stop_reason:
            report.stop_reason = s.stop_reason
            return report
        jhist = _jev_history(jfile)
        jprior = jev.prior_cost(jhist)
        jevs = jev_by_item(jhist, entries)
    g, gitems = gem_stage()
    report.gemini_targets = len(g)
    est = estimate_cost(gitems, model, ghist)
    report.gemini_pending, report.gemini_est_usd = est["calls"], est["cost_usd"]
    gcap = max_cost_usd - jprior
    if report.gemini_pending:
        if gcap <= 0 or gprior + est["cost_usd"] > gcap:
            report.stop_reason = "cost_cap"
            report.notes.append("Gemini 추정 비용이 상한을 넘어 호출하지 않았다")
            return report
        out_dir.mkdir(parents=True, exist_ok=True)
        gs = await run_correctness(
            gitems, out_dir, model, ReviewConfig(max_cost_usd=gcap),
            llm_factory=llm_factory, progress=progress,
        )  # fmt: skip
        report.spent_usd += gs.spent_usd
        if gs.stop_reason:
            report.stop_reason = gs.stop_reason
            return report
    gem = gemini_by_item(read_rows(out_dir / JUDGED_FILE), entries, model.family)
    final = finalize(entries, jevs, gem, fp)
    report.undecided = sum(1 for r in final if (r.get("outcome") or {}).get("success") is None)
    missing = sum(
        1
        for e, r in zip(entries, final, strict=True)
        if e.target and not e.empty and (r.get("outcome") or {}).get("success") is None
    )
    if missing:  # 호출 실패로 판정이 빠졌다. 빈 답의 null은 정상이다
        report.stop_reason = "missing_verdicts"
        report.notes.append(f"판정이 빠진 에피소드 {missing}건")
        return report
    report.written = write_graded(graded_path(results), final)
    return report


def write_graded(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    """같은 폴더의 고유한 임시 파일을 완성한 뒤 기존 파일을 덮지 않고 게시한다."""
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
            delete=False,
        ) as fh:  # fmt: skip
            tmp = Path(fh.name)
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.link(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return path
