"""rag-v1 생성 CLI.

```bash
cd backend
uv run python -m orbit.evaldata.rag_v1 skeleton [--seed N] [--out DIR]
uv run python -m orbit.evaldata.v1.realize ... # 본문 실현은 v1 도구를 그대로 쓴다
uv run python -m orbit.evaldata.rag_v1 tasks (--skip-paraphrase | --max-cost-usd X [--dry-run])
uv run python -m orbit.evaldata.rag_v1 review
uv run python -m orbit.evaldata.rag_v1 stats
uv run python -m orbit.evaldata.rag_v1 seal --review-note 문자열 # test 봉인
uv run python -m orbit.evaldata.rag_v1 verify-seal
```

기본 출력은 `eval/datasets/orbit-eval/rag-v1/`다. 같은 시드·git SHA·저장된 표현 변형이면 산출물은
바이트 단위로 같다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from orbit.evaldata import dataset as ds
from orbit.evaldata.generate import git_sha
from orbit.evaldata.rag_v1 import seal
from orbit.evaldata.rag_v1.build import CONFIG_SHA256, build
from orbit.evaldata.rag_v1.model import Plan
from orbit.evaldata.rag_v1.questions import (
    QuestionError,
    build_tasks,
    shared_evidence_problems,
    sources_for,
    world_problems,
)
from orbit.evaldata.rag_v1.review import render_review
from orbit.evaldata.rag_v1.stats import LATE_NOTE, LONG_CHARS, MIN_LONG_RATIO, compute_stats
from orbit.evaldata.schema import Counts, Manifest, SplitRatio, Task
from orbit.evaldata.v1 import paraphrase as pp
from orbit.evaldata.v1.build import dump_json, skeleton_json
from orbit.evaldata.v1.realize_models import FamilyModel, ModelConfigError, load_models
from orbit.evaldata.v1.skeleton import Skeleton

DEFAULT_SEED = 20261121
DEFAULT_DIR = Path(__file__).resolve().parents[5] / "eval" / "datasets" / "orbit-eval" / "rag-v1"
EXIT_OK, EXIT_USAGE, EXIT_COST_CAP, EXIT_FATAL, EXIT_PLAN = 0, 2, 3, 4, 5
PLAN_FILE, SKELETON_FILE, GRAPH_FILE = "rag-plan.json", "skeleton.json", "graph_gold.json"
STATS_FILE, REVIEW_FILE = "rag-stats.json", "review-packet.md"
NOTES = (
    "rag-v1: RAG 아키텍처 비교(ADR-0036)용 합성 코퍼스·질문 세트. 정답은 message_set(메일 ID 집합)"
    "이다. 집합 정답은 일부만 맞아도 오답이라 엄격하다. 문서 엔티티는 두지 않았다."
)


def _load_plan(d: Path) -> Plan:
    return Plan.model_validate_json((d / PLAN_FILE).read_text(encoding="utf-8"))


def _load_skeleton(d: Path) -> Skeleton:
    return Skeleton.model_validate_json((d / SKELETON_FILE).read_text(encoding="utf-8"))


def _bodies(d: Path) -> dict[str, dict[str, str]] | None:
    """world.json이 있으면 계정 ID -> (메일 ID -> 본문)."""
    path = d / ds.WORLD
    if not path.exists():
        return None
    world = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: {m["id"]: str(m.get("body_text", "")) for m in a.get("messages", [])}
        for k, a in world.get("accounts", {}).items()
    }


def _flat(bodies: dict[str, dict[str, str]] | None) -> dict[str, str] | None:
    return None if bodies is None else {m: t for a in bodies.values() for m, t in a.items()}


def _world_for(sk: Skeleton) -> dict[str, Any]:
    """검증용 최소 세계(skeleton 구조만)."""
    return {
        "accounts": {
            k: {"messages": [{"id": m.id} for m in a.messages], "events": [], "calendars": []}
            for k, a in sk.accounts.items()
        }
    }


def cmd_skeleton(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    out = Path(args.out)
    sha = args.git_sha if args.git_sha is not None else git_sha(Path(__file__).parent)
    sk, plan = build(args.seed, sha)
    out.mkdir(parents=True, exist_ok=True)
    (out / SKELETON_FILE).write_text(skeleton_json(sk), encoding="utf-8")
    (out / PLAN_FILE).write_text(dump_json(plan.model_dump(mode="json")), encoding="utf-8")
    graph = {
        "schema_version": plan.schema_version,
        "seed": plan.seed,
        "generator_git_sha": sha,
        "triples": [t.model_dump(mode="json") for t in plan.triples],
    }
    (out / GRAPH_FILE).write_text(dump_json(graph), encoding="utf-8")
    mails = {a: len(acc.messages) for a, acc in sk.accounts.items()}
    echo(f"skeleton 생성: 계정 {len(mails)}개, 메일 {mails}, 질문 설계 {len(plan.blueprints)}개")
    echo(f"graph_gold 삼중항 {len(plan.triples)}개 -> {out}")
    return EXIT_OK


def _paraphrases(
    plan: Plan, out: Path, args: argparse.Namespace, echo: Callable[[str], None]
) -> tuple[dict[str, str], dict[str, Any], int | None]:
    sources = sources_for(plan)
    models: dict[str, FamilyModel] = load_models(args.models_config, openai_route=args.openai_route)
    path = out / pp.PARAPHRASE_FILE
    pp.check_model_configs(pp.read_all_rows(path), models)
    rows = pp.read_rows(path)
    pending = [s for s in sources if not pp.usable_row(rows.get(s.origin_id), s)]
    spent = pp.total_cost(pp.read_all_rows(path))
    echo(f"표현 변형: 원본 {len(sources)}개 중 남은 {len(pending)}개. 누적 지출 ${spent:.4f}")
    if pending:
        est = pp.estimate_cost(pending, models)
        for fam, e in est["families"].items():
            echo(
                f"  {fam}: 원본 {e['sources']}개, 입력 {e['input_tokens']:,} / 출력 "
                f"{e['output_tokens']:,} 토큰, 예상 ${e['cost_usd']:.4f}"
            )
        echo(f"예상 합계 ${est['total_cost_usd']:.4f} (누적 상한 ${args.max_cost_usd})")
        if args.dry_run:
            return {}, {}, EXIT_OK
        summary = asyncio.run(
            pp.run_paraphrases(
                sources,
                len(sources),
                out,
                models,
                pp.ParaphraseConfig(max_cost_usd=args.max_cost_usd, concurrency=args.concurrency),
                progress=echo,
            )
        )
        echo(f"요청 {summary.requests}회, 지출 ${summary.spent_usd:.4f}, 통과 {summary.ok}건")
        if summary.stop_reason == "cost_cap":
            echo(f"비용 상한 ${args.max_cost_usd}에 닿아 멈췄다. 같은 명령으로 이어서 실행한다")
            return {}, {}, EXIT_COST_CAP
        if summary.stop_reason == "fatal":
            print(f"영구 오류로 멈췄다: {summary.fatal_message}", file=sys.stderr)
            return {}, {}, EXIT_FATAL
        rows = pp.read_rows(path)
    elif args.dry_run:
        echo("추가 호출이 필요 없다(예상 비용 $0)")
        return {}, {}, EXIT_OK
    texts = {
        s.origin_id: str(rows[s.origin_id]["text"])
        for s in sources
        if pp.usable_row(rows.get(s.origin_id), s) and rows[s.origin_id]["status"] == "ok"
    }
    info = {
        "enabled": True,
        "prompt_version": pp.PROMPT_VERSION,
        "ok": len(texts),
        "cost_usd": round(pp.total_cost(pp.read_all_rows(path)), 6),
        "models": {
            f: {"model_id": m.model_id, "route": m.route, "temperature": m.temperature}
            for f, m in sorted(models.items())
        },
    }
    return texts, info, None


def cmd_tasks(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    out = Path(args.out)
    plan, sk = _load_plan(out), _load_skeleton(out)
    texts: dict[str, str] = {}
    info: dict[str, Any] = {
        "enabled": False,
        "note": "--skip-paraphrase: 템플릿 질문만 썼다",
    }
    if not args.skip_paraphrase:
        texts, info, code = _paraphrases(plan, out, args, echo)
        if code is not None:
            return code
    tasks = build_tasks(plan, texts)
    bodies = _bodies(out)
    problems = shared_evidence_problems(tasks)
    verified = bodies is not None
    ratio: float | None = None
    if bodies is None:
        if not args.unverified_bodies:
            print(
                "world.json이 없어 정답을 본문과 대조할 수 없다. realize --assemble 뒤에 실행한다."
                " 테스트용으로만 --unverified-bodies를 쓴다",
                file=sys.stderr,
            )
            return EXIT_PLAN
    else:
        problems += world_problems(plan, bodies)
        stats = compute_stats(plan, tasks, sk, _flat(bodies))
        ratio = stats["long_evidence_ratio"]
        if ratio < MIN_LONG_RATIO:
            problems.append(
                f"긴 근거(>{LONG_CHARS}자) 비율 {ratio}이 기준 {MIN_LONG_RATIO} 미만이다"
            )
    world = _world_for(sk)
    n = len(tasks)
    dev = sum(1 for t in tasks if t.split == "dev")
    sources = sorted({str(t.difficulty["question_source"]) for t in tasks})
    manifest = Manifest(
        name="orbit-eval-rag",
        version="rag-v1",
        generator_git_sha=plan.generator_git_sha,
        seed=plan.seed,
        created_with="structure+llm" if info.get("enabled") else "templates",
        split_ratio=SplitRatio(dev=round(dev / n, 4), test=round((n - dev) / n, 4)),
        counts=Counts(
            users=len(sk.accounts),
            messages=sum(len(a.messages) for a in sk.accounts.values()),
            events=0,
            tasks=n,
        ),
        notes=NOTES,
        details={
            "schema_version": plan.schema_version,
            "config_sha256": CONFIG_SHA256,
            "skeleton_sha256": plan.skeleton_sha256,
            "paraphrase": info,
            "bodies_verified": verified,
            "bodies_note": "정답을 실현된 본문과 대조했다" if verified else "본문 미검증",
            "long_evidence_ratio": ratio,
            "fact_late_position": LATE_NOTE,
            "question_sources": sources,
            "paraphrased_tasks": sum(
                1 for t in tasks if t.difficulty["question_source"] == "paraphrase"
            ),
        },
    )
    problems += ds.validate_dataset(manifest, world, tasks)
    if problems:
        for p in problems:
            print(f"검증 실패: {p}", file=sys.stderr)
        return EXIT_PLAN
    ds.write_tasks(out, tasks)
    ds.write_manifest(out, manifest)
    echo(f"tasks 생성: {n}개(dev {dev}, test {n - dev}) -> {out}")
    return EXIT_OK


def _read_tasks(out: Path) -> list[Task]:
    lines = (out / ds.TASKS).read_text(encoding="utf-8").splitlines()
    return [Task.model_validate_json(x) for x in lines if x.strip()]


def cmd_review(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    out = Path(args.out)
    text = render_review(
        _load_plan(out), _read_tasks(out), _load_skeleton(out), _flat(_bodies(out))
    )
    (out / REVIEW_FILE).write_text(text, encoding="utf-8")
    echo(f"검수 꾸러미 -> {out / REVIEW_FILE}")
    return EXIT_OK


def cmd_stats(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    out = Path(args.out)
    stats = compute_stats(
        _load_plan(out), _read_tasks(out), _load_skeleton(out), _flat(_bodies(out))
    )
    (out / STATS_FILE).write_text(ds.dumps_json(stats), encoding="utf-8")
    echo(ds.dumps_json(stats).rstrip())
    return EXIT_OK


def main(argv: list[str] | None = None, echo: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="Orbit rag-v1 코퍼스·질문 세트 생성기")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("skeleton", "tasks", "review", "stats", "seal", "verify-seal"):
        p = sub.add_parser(name)
        p.add_argument("--out", default=str(DEFAULT_DIR), help="데이터셋 폴더")
        if name == "seal":
            p.add_argument("--review-note", required=True)
        if name == "skeleton":
            p.add_argument("--seed", type=int, default=DEFAULT_SEED)
            p.add_argument("--git-sha", default=None)
        if name == "tasks":
            p.add_argument("--skip-paraphrase", action="store_true")
            p.add_argument("--max-cost-usd", type=float, default=None)
            p.add_argument("--dry-run", action="store_true")
            p.add_argument(
                "--unverified-bodies", action="store_true", help="world.json 없이(테스트용)"
            )
            p.add_argument("--models-config", default=None)
            p.add_argument("--openai-route", choices=("openai", "openrouter"), default=None)
            p.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)
    if (
        args.cmd == "tasks"
        and not args.skip_paraphrase
        and (
            args.max_cost_usd is None
            or not math.isfinite(args.max_cost_usd)
            or args.max_cost_usd <= 0
        )
    ):
        print(
            "--max-cost-usd(0보다 큼)가 필요하다. LLM 없이 만들려면 --skip-paraphrase",
            file=sys.stderr,
        )
        return EXIT_USAGE
    handlers = {
        "skeleton": cmd_skeleton,
        "tasks": cmd_tasks,
        "review": cmd_review,
        "stats": cmd_stats,
        "seal": seal.cmd_seal,
        "verify-seal": seal.cmd_verify,
    }
    try:
        return handlers[args.cmd](args, echo)
    except (QuestionError, ModelConfigError) as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return EXIT_PLAN
    except ValidationError as exc:
        names = sorted({".".join(str(p) for p in e["loc"]) or "(전체)" for e in exc.errors()})
        print(f"입력 검증 오류: {', '.join(names)}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"입출력 오류: {type(exc).__name__}", file=sys.stderr)
        return EXIT_USAGE
