"""평가 러너 CLI.

    python -m orbit.evalrunner run --dataset <dir> --split dev --sut <이름> \
        --repeats 3 --out <jsonl>
    python -m orbit.evalrunner stats <jsonl> [--compare <jsonl>]

종료 코드: 0 정상, 2 사용 오류(봉인된 test 분할, 파일 충돌, 잘못된 입력), 3 누적 상한으로 중단.
이 CLI는 실제 LLM을 부르지 않는다. `--llm-script`가 없으면 LLM 호출은 모두 실패한다.
"""

import argparse
import asyncio
import json
import subprocess  # noqa: S404  git SHA 조회에만 쓴다
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orbit.connectors.sim.faults import FaultPlan
from orbit.evalrunner import stats
from orbit.evalrunner.episode import Budget
from orbit.evalrunner.llm_script import load_script, scripted_llm_factory
from orbit.evalrunner.runner import (
    ExperimentBudget,
    FaultPlanSpec,
    SealedSplitError,
    estimate_experiment,
    run_experiment,
)
from orbit.evalrunner.suts import UnknownSutError, create_sut, sut_names
from orbit.evalrunner.task_source import DatasetError, load_dataset

SEAL_FLAG = "--i-understand-this-uses-the-sealed-test-split"


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def _git_state(out_dir: Path | None = None) -> tuple[str, bool]:
    """(HEAD SHA, 작업 트리에 추적 파일 변경이 있는가). git을 쓸 수 없으면 ("unknown", True).

    추적하지 않는 파일은 `backend/src`, `eval/` 아래에 있을 때만 변경으로 본다(`out_dir` 제외).
    """
    sha = (_git("rev-parse", "HEAD") or "").strip() or "unknown"
    status = _git("status", "--porcelain", "--untracked-files=no")
    if status is None or status.strip():
        return sha, True
    # 코드가 있는 곳(`backend/src`, `eval/`)의 추적하지 않는 파일도 변경으로 본다.
    # 결과 출력 폴더는 제외한다.
    specs = [":(top)backend/src", ":(top)eval"]
    top = (_git("rev-parse", "--show-toplevel") or "").strip()
    if out_dir is not None and top:
        try:
            rel = out_dir.resolve().relative_to(Path(top).resolve())
        except ValueError:
            rel = None
        if rel is not None and str(rel) != ".":
            specs.append(f":(top,exclude){rel.as_posix()}")
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", *specs)
    return sha, untracked is None or bool(untracked.strip())


def _load_fault_plan(path: str | None) -> FaultPlanSpec:
    if path is None:
        return FaultPlanSpec()
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    name = str(data.pop("name", Path(path).stem))
    return FaultPlanSpec(name=name, plan=FaultPlan.from_dict(data))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orbit.evalrunner")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="평가를 실행한다")
    run.add_argument("--dataset", required=True)
    run.add_argument("--split", required=True, choices=["dev", "test"])
    run.add_argument("--sut", required=True, help=f"등록된 이름: {', '.join(sut_names())}")
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--out", required=True)
    run.add_argument("--seed", type=int, default=0, help="실험 시드이자 표본 추출 시드")
    run.add_argument("--sample", type=int, default=None, help="과제 N개만 시드 고정으로 뽑는다")
    run.add_argument(
        "--estimate", type=int, default=None, metavar="N",
        help="과제 N개를 1회씩 실행해 전체 비용·토큰을 추정한다(결과는 <out 이름>.estimate.jsonl)",
    )  # fmt: skip
    run.add_argument("--max-steps", type=int, default=Budget.max_steps)
    run.add_argument("--max-episode-tokens", type=int, default=Budget.max_tokens)
    run.add_argument("--max-seconds", type=float, default=Budget.max_seconds)
    run.add_argument("--max-cost-usd", type=float, default=None, help="실험 누적 비용 상한")
    run.add_argument("--max-total-tokens", type=int, default=None, help="실험 누적 토큰 상한")
    run.add_argument("--fault-plan", default=None, help='{"name": ..., "rules": [...]} JSON 파일')
    run.add_argument("--model-id", default="unspecified")
    run.add_argument("--prompt-version", default="unspecified")
    run.add_argument("--experiment-id", default=None)
    run.add_argument("--llm-script", default=None, help="과제별 ScriptedLLM 응답 JSON")
    run.add_argument(SEAL_FLAG, dest="allow_test", action="store_true")

    st = sub.add_parser("stats", help="결과 JSONL 통계")
    st.add_argument("results")
    st.add_argument("--compare", default=None, help="짝지은 차이를 낼 다른 결과 (a - b, a가 첫째)")
    st.add_argument("--include-incomplete", action="store_true", help="반복이 부족한 과제도 포함")
    st.add_argument(
        "--reject-model-mismatch", action="store_true", help="선언 모델과 실제 모델이 다르면 거부"
    )
    st.add_argument("--resamples", type=int, default=stats.DEFAULT_RESAMPLES)
    st.add_argument("--seed", type=int, default=stats.DEFAULT_SEED)
    return p


def _run(args: argparse.Namespace) -> int:
    if args.split == "test" and not args.allow_test:
        raise SealedSplitError("test 분할 봉인")  # 출력 파일을 만들기 전에 거부한다
    dataset = load_dataset(args.dataset)
    out = Path(args.out)
    sink_path = out.with_name(out.stem + ".estimate.jsonl") if args.estimate is not None else out
    if sink_path.exists():
        print(f"이미 있는 파일에는 쓰지 않는다: {sink_path}", file=sys.stderr)
        return 2
    llm_factory = load_script(args.llm_script) if args.llm_script else scripted_llm_factory({})
    sha, dirty = _git_state(out.parent)  # 출력 파일을 만들기 전에 읽는다
    common: dict[str, Any] = {
        "split": args.split,
        "sut_factory": lambda: create_sut(args.sut),
        "llm_factory": llm_factory,
        "seed": args.seed,
        "budget": Budget(args.max_steps, args.max_episode_tokens, args.max_seconds),
        "experiment_budget": ExperimentBudget(args.max_cost_usd, args.max_total_tokens),
        "fault": _load_fault_plan(args.fault_plan),
        "model_id": args.model_id,
        "prompt_version": args.prompt_version,
        "git_sha": sha,
        "git_dirty": dirty,
        "experiment_id": args.experiment_id,
        "allow_sealed_test": args.allow_test,
    }
    with sink_path.open("x", encoding="utf-8") as fh:

        def sink(result: Any) -> None:
            fh.write(result.to_json_line() + "\n")
            fh.flush()

        if args.estimate is not None:
            est, summary = asyncio.run(
                estimate_experiment(
                    dataset, n=args.estimate, repeats=args.repeats, sink=sink, **common
                )
            )
            print(json.dumps(est.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            summary = asyncio.run(
                run_experiment(
                    dataset,
                    repeats=args.repeats,
                    sample=args.sample,
                    sink=sink,
                    **common,
                )
            )
    done = len(summary.results)
    unknown = summary.cost_unknown_episodes
    note = f" (비용 불명 에피소드 {unknown}개)" if unknown else ""
    print(
        f"experiment_id={summary.experiment_id} episodes={done}/{summary.episodes_planned} "
        f"tokens={summary.total_tokens} cost_usd={summary.total_cost_usd:.6f}{note}"
    )
    if summary.stopped_reason is not None:
        print(f"누적 상한으로 중단했다: {summary.stopped_reason}", file=sys.stderr)
        return 3
    return 0


def _stats(args: argparse.Namespace) -> int:
    reject = args.reject_model_mismatch
    records = stats.load_results(args.results, reject_model_mismatch=reject)
    kw = {
        "resamples": args.resamples,
        "seed": args.seed,
        "include_incomplete": args.include_incomplete,
    }
    rate = stats.success_rate(records, **kw)
    report: dict[str, Any] = {
        "success_rate": rate.__dict__,
        "pass_k": stats.pass_k(records).__dict__,
        "failure_distribution": stats.failure_distribution(records),
        "state_diff_mismatch_episodes": stats.state_diff_mismatch_count(records),
        "model_mismatch_episodes": stats.model_mismatch_count(records),
    }
    if args.compare:
        other = stats.load_results(args.compare, reject_model_mismatch=reject)
        report["paired_difference_a_minus_b"] = stats.paired_difference(
            records, other, **kw
        ).__dict__
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args) if args.command == "run" else _stats(args)
    except SealedSplitError:
        print(f"test 분할은 봉인돼 있다. 실행하려면 {SEAL_FLAG}를 명시한다.", file=sys.stderr)
        return 2
    except (DatasetError, stats.ResultsError, UnknownSutError) as exc:
        # 이 예외들의 메시지는 위치·이름만 담도록 직접 만든 것이다.
        print(f"오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        # 외부 라이브러리 오류(pydantic 등)의 메시지에는 입력 원문이 들어갈 수 있어 이름만 쓴다.
        print(f"오류: {type(exc).__name__}", file=sys.stderr)
        return 2
