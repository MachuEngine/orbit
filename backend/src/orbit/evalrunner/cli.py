"""평가 러너 CLI.

    python -m orbit.evalrunner run --dataset <dir> --split dev --sut <이름> \
        --repeats 3 --out <jsonl>
    python -m orbit.evalrunner stats <jsonl> [--compare <jsonl>]
    python -m orbit.evalrunner judge-input <jsonl> --out <jsonl>
    python -m orbit.evalrunner judge run|agreement|human-sample ...   (judge.py 참고)

종료 코드: 0 정상, 2 사용 오류(봉인된 test 분할, 파일 충돌, 잘못된 입력), 3 누적 상한으로 중단.
기본은 실제 LLM을 부르지 않는다. `--llm-script`가 없으면 LLM 호출은 모두 실패한다.
`--live-llm`을 주면 `ORBIT_LLM_*` 환경 변수의 제공자(ADR-0014)로 실제 호출한다(비용 발생).
"""

import argparse
import asyncio
import json
import os
import subprocess  # noqa: S404  git SHA 조회에만 쓴다
import sys
import tempfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.engine import make_url

from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.core.config import get_llm_settings
from orbit.evaldata.dataset import read_review
from orbit.evalrunner import judge, metrics_v1, stats
from orbit.evalrunner.episode import Budget
from orbit.evalrunner.llm_script import load_script, scripted_llm_factory
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.dryrun import dry_run_report
from orbit.evalrunner.retrieval.embed_cache import BudgetExceeded as EmbedBudgetExceeded
from orbit.evalrunner.retrieval.embed_cache import CachingEmbedder
from orbit.evalrunner.retrieval.indexer import IndexBuildError
from orbit.evalrunner.runflow_env import EvalDatabaseSettings, RunflowEnvironment
from orbit.evalrunner.runflow_index import (
    AsOfBeforeIndexedMail,
    EvalIndexProvider,
    build_eval_indexes,
    check_as_of,
)
from orbit.evalrunner.runflow_sut import VARIANTS
from orbit.evalrunner.runner import (
    ExperimentBudget,
    FaultPlanSpec,
    LLMFactory,
    SealedSplitError,
    estimate_experiment,
    run_experiment,
    select_tasks,
)
from orbit.evalrunner.suts import DATABASE_SUTS, UnknownSutError, create_sut, sut_names
from orbit.evalrunner.task_source import Dataset, DatasetError, load_dataset
from orbit.llm.factory import build_llm, llm_config_info
from orbit.runflow.patterns import PATTERNS

SEAL_FLAG = "--i-understand-this-uses-the-sealed-test-split"


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def _git_state(out_dir: Path | None = None, repo: Path | None = None) -> tuple[str, bool]:
    """(HEAD SHA, 작업 트리에 추적 파일 변경이 있는가). git을 쓸 수 없으면 ("unknown", True).

    추적하지 않는 파일은 `backend/src`, `eval/` 아래에 있을 때만 변경으로 본다(`out_dir` 제외).
    """
    # 실행 위치(cwd)가 아니라 이 패키지가 있는 저장소를 본다. `repo`는 테스트용 재정의다.
    start = repo or Path(__file__).resolve().parent
    top_out = _git(start, "rev-parse", "--show-toplevel")
    if top_out is None or not top_out.strip():
        return "unknown", True
    top = Path(top_out.strip())
    sha = (_git(top, "rev-parse", "HEAD") or "").strip() or "unknown"
    status = _git(top, "status", "--porcelain", "--untracked-files=no")
    if status is None or status.strip():
        return sha, True
    # 코드가 있는 곳(`backend/src`, `eval/`)의 추적하지 않는 파일도 변경으로 본다.
    # 결과 출력 폴더는 제외한다.
    specs = [":(top)backend/src", ":(top)eval"]
    if out_dir is not None:
        try:
            rel = out_dir.resolve().relative_to(top.resolve())
        except ValueError:
            rel = None
        if rel is not None and str(rel) != ".":
            specs.append(f":(top,exclude){rel.as_posix()}")
    untracked = _git(top, "ls-files", "--others", "--exclude-standard", "--", *specs)
    return sha, untracked is None or bool(untracked.strip())


def _load_fault_plan(path: str | None) -> FaultPlanSpec:
    if path is None:
        return FaultPlanSpec()
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    name = str(data.pop("name", Path(path).stem))
    return FaultPlanSpec(name=name, plan=FaultPlan.from_dict(data))


def _review_excluded(dataset_dir: str | Path) -> frozenset[str]:
    """review.jsonl에서 사람 검수로 제외된 과제 ID. 이 과제는 데이터셋에서 이미 빠져 있다."""
    return frozenset(i for i, e in read_review(dataset_dir).items() if e.decision == "exclude")


def _smoke_subset(dataset: Dataset, skip: Collection[str] = frozenset()) -> Dataset:
    """manifest의 `smoke_task_ids`에 있는 과제만 남긴다.

    `skip`(검수 제외 과제 ID)에 있는 id는 데이터셋에 없어도 건너뛴다. 그 밖에 데이터셋에 없는 id가
    있거나 목록이 없으면 `DatasetError`다.
    """
    ids = dataset.manifest.get("smoke_task_ids")
    if not ids:
        raise DatasetError("manifest에 smoke_task_ids가 없다(스모크 목록이 없는 데이터셋)")
    by_id = {t.task_id: t for t in dataset.tasks}
    missing = sorted(set(ids) - set(by_id) - set(skip))
    if missing:
        raise DatasetError(f"smoke_task_ids에 데이터셋에 없는 과제가 있다: {missing[:5]}")
    chosen = set(ids) & set(by_id)
    tasks = tuple(t for t in dataset.tasks if t.task_id in chosen)
    if not tasks:
        raise DatasetError("스모크 과제가 남지 않았다(모두 검수에서 제외됐다)")
    if any(t.split != "dev" for t in tasks):
        raise DatasetError("스모크 목록에 dev가 아닌 과제가 있다(test 봉인 규칙 위반)")
    return replace(dataset, tasks=tasks)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orbit.evalrunner")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="평가를 실행한다")
    run.add_argument("--dataset", required=True)
    run.add_argument("--split", required=True, choices=["dev", "test"])
    run.add_argument(
        "--subset", choices=["smoke"], default=None,
        help="smoke: manifest의 smoke_task_ids만 실행한다(dev 분할만, "
        "ADR-0026 §5). CI 회귀 확인용이며 "
        "비교 실험의 판단 근거로 쓰지 않는다",
    )  # fmt: skip
    run.add_argument("--sut", required=True, help=f"등록된 이름: {', '.join(sut_names())}")
    run.add_argument(
        "--index", choices=["off", "on"], default="off",
        help="runflow_v0의 색인 검색 모드. off(기본): search_indexed_mail은 index_unavailable. "
        "on: 실행 전에 과제 계정마다 평가용 색인을 만들거나 재사용하고(ADR-0028 인덱서, 평가 DB "
        "orbit_eval) 그 색인을 고정 시점 스냅샷으로 검색한다. runflow_v0가 아니면 오류",
    )  # fmt: skip
    run.add_argument(
        "--embedder", choices=["openai", "fake"], default="openai",
        help="--index on의 임베딩 방식(openai는 ORBIT_OPENAI_API_KEY 필요, 비용 발생)",
    )  # fmt: skip
    run.add_argument(
        "--max-embed-cost-usd", type=float, default=None,
        help="--index on의 임베딩 누적 비용 상한(색인 + 질의 임베딩). 넘을 것 같으면 중단(코드 3)",
    )  # fmt: skip
    run.add_argument(
        "--price-per-mtok", type=float, default=None, help="임베딩 1M 토큰당 달러(재정의)"
    )
    run.add_argument(
        "--embed-cache-dir", default=None, help="임베딩 캐시 폴더(기본 runs/embed-cache)"
    )
    run.add_argument(
        "--embedding-stats-out", default=None,
        help="--index on의 최종 임베딩 통계를 별도 JSON에 저장한다(색인+질의, 덮어쓰기 금지). "
        "dry-run에서는 쓰지 않는다. 강제 종료로 파일이 없으면 비용은 미관측이다",
    )  # fmt: skip
    run.add_argument(
        "--index-dry-run", action="store_true",
        help="--index on의 색인 비용만 추정해 출력하고 끝낸다(임베딩 호출·DB 접근 없음). "
        "에피소드 중 모델이 보내는 검색 질의의 임베딩 비용은 추정에 포함하지 않는다",
    )  # fmt: skip
    run.add_argument(
        "--runflow-variant", action="append", default=[], choices=sorted(VARIANTS),
        help="runflow_v0 전용 원인 확인 변형(ADR-0023 §10). 반복해 여러 개를 켤 수 있다: "
        "facts(facts-v1, 프롬프트에 [사실 확인] 단락), reasoning-rt(추론 항목 되돌리기). "
        "runflow_v0가 아니면 오류",
    )  # fmt: skip
    run.add_argument(
        "--pattern", choices=list(PATTERNS), default=None,
        help="runflow_v0 전용 Agent 패턴(ADR-0030). react(기본, 현재 runflow), "
        "plan_execute(계획 1회 + 오류 시 재계획 1회), "
        "fixed_workflow(의도 분류 + 범주별 도구 제한). "
        "runflow_v0가 아니면 오류",
    )  # fmt: skip
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
    run.add_argument(
        "--live-llm", action="store_true",
        help="ORBIT_LLM_* 설정의 실제 LLM을 호출한다(비용 발생). --llm-script와 함께 쓸 수 없다",
    )  # fmt: skip
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

    ji = sub.add_parser(
        "judge-input",
        help="서술형 근거성 Judge용 입력(답 문장·도구 결과 본문·과제 ID)을 JSONL로 내보낸다. "
        "Judge 호출은 하지 않는다(ADR-0029 §3)",
    )
    ji.add_argument("results")
    ji.add_argument("--out", required=True)
    judge.add_parser(sub)
    return p


def _live_llm(declared_model_id: str) -> tuple[LLMFactory, dict[str, Any], str] | None:
    """환경 변수에서 실제 LLM 팩토리·설정 지문·모델 ID를 만든다. 실패하면 None(종료 코드 2)."""
    try:
        settings = get_llm_settings()
    except ValidationError as exc:
        # 오류 항목의 입력값에는 키가 들어 있을 수 있어 필드 이름만 출력한다.
        names = sorted({".".join(str(p) for p in e["loc"]) or "(설정 전체)" for e in exc.errors()})
        print(f"LLM 설정 오류 (확인할 항목: {', '.join(names)})", file=sys.stderr)
        return None
    if declared_model_id not in ("unspecified", settings.llm_model_id):
        print("--model-id가 ORBIT_LLM_MODEL_ID와 다르다", file=sys.stderr)
        return None
    llm = build_llm(settings)
    return (lambda task, repeat: llm), llm_config_info(settings), settings.llm_model_id


def _database_env(sut: str) -> RunflowEnvironment | None:
    """DB를 쓰는 평가 대상이면 `ORBIT_EVAL_DATABASE_URL`로 환경을 만든다. 쓸 수 없으면 None."""
    if sut not in DATABASE_SUTS:
        return None
    try:
        url = EvalDatabaseSettings().eval_database_url.get_secret_value()
    except ValidationError:
        print(f"{sut}에는 ORBIT_EVAL_DATABASE_URL(평가 DB, 앱 역할)이 필요하다", file=sys.stderr)
        return None
    if (make_url(url).database or "") == "orbit":
        print(
            "평가는 개발 DB(orbit)에서 실행하지 않는다. 전용 DB(orbit_eval)를 쓴다", file=sys.stderr
        )
        return None
    return RunflowEnvironment(url)


@dataclass
class _IndexSetup:
    """`--index on`의 실행 전 준비 값. `build`는 색인을 만들고 제공자를 돌려준다."""

    args: argparse.Namespace
    accounts: list[str]
    world_snapshot: Mapping[str, Any]
    world_sha256: str
    model_id: str
    cache_dir: Path
    urls: tuple[str, str] | None = None
    embedder: CachingEmbedder | None = None

    def dry_run(self) -> dict[str, Any]:
        """색인 비용 추정. 임베딩 호출도 DB 접근도 하지 않는다(질의 임베딩은 추정에 없다)."""
        world = SimWorld.load(dict(self.world_snapshot), FixedClock(worldmail.INDEX_NOW))
        report = dry_run_report(
            world=world,
            sizes=self.accounts,
            accounts={a: a for a in self.accounts},
            query_texts=[],
            model_id=self.model_id,
            cache_dir=self.cache_dir,
            price_per_mtok=self.args.price_per_mtok,
            needs_query_embedding=False,
        )
        report["note"] = (
            "과제 계정별 색인만 추정한다. 평가 DB에 같은 지문의 색인이 있으면 임베딩이 필요 없다. "
            "에피소드 중 모델이 보내는 검색 질의의 임베딩은 포함하지 않는다."
        )
        return report

    async def build(self) -> tuple[EvalIndexProvider, float]:
        assert self.embedder is not None and self.urls is not None  # noqa: S101  준비 단계가 채운다
        return await build_eval_indexes(
            world_snapshot=self.world_snapshot,
            world_sha256=self.world_sha256,
            accounts=self.accounts,
            app_url=self.urls[0],
            owner_url=self.urls[1],
            embedder=self.embedder,
        )


def _index_setup(args: argparse.Namespace, dataset: Dataset) -> _IndexSetup | None:
    """`--index on` 준비. 설정이 잘못됐으면 메시지를 출력하고 None(종료 코드 2)."""
    # 검색 평가 CLI가 이 모듈을 불러오므로 순환을 피하려고 함수 안에서 가져온다.
    from orbit.evalrunner.retrieval import cli as retrieval_cli

    sample = args.estimate if args.estimate is not None else args.sample
    tasks = select_tasks(dataset.tasks_for(args.split), sample, args.seed)
    accounts = sorted({t.account_id for t in tasks})
    # 색인은 고정 스냅샷이므로 기준 시각 뒤의 메일이 색인에 있으면 실행하지 않는다
    check_as_of(SimWorld.load(dict(dataset.world_snapshot), FixedClock(worldmail.INDEX_NOW)), tasks)
    world_path = Path(args.dataset) / "world.json"
    model_id, fake, embed_settings = retrieval_cli._embedder_parts(args.embedder)
    setup = _IndexSetup(
        args=args,
        accounts=accounts,
        world_snapshot=dataset.world_snapshot,
        world_sha256=worldmail.file_sha256(world_path),
        model_id=model_id,
        cache_dir=Path(args.embed_cache_dir or retrieval_cli.DEFAULT_CACHE_DIR),
    )
    if args.index_dry_run:
        return setup
    try:
        setup.urls = retrieval_cli._eval_urls()
        inner = fake if fake is not None else retrieval_cli._build_openai(embed_settings)
    except retrieval_cli.UsageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return None
    setup.embedder = CachingEmbedder(
        inner,
        setup.cache_dir,
        max_cost_usd=args.max_embed_cost_usd,
        price_per_mtok=args.price_per_mtok,
    )
    return setup


def _print_index_summary(
    provider: EvalIndexProvider, embedder: CachingEmbedder | None, seconds: float
) -> None:
    """색인 준비 결과를 표준 오류에 한 줄씩 낸다(계정 ID·건수만)."""
    for account, h in sorted(provider.handles.items()):
        state = "재사용" if h.reused else "새로 만듦"
        print(
            f"색인 {account}: {state} mails={h.indexed_mails} chunks={h.chunks} "
            f"embedded_chunks={h.embedded_chunks} fingerprint={h.fingerprint[:12]}",
            file=sys.stderr,
        )
    emb = embedder.stats() if embedder is not None else {}
    print(
        f"색인 준비 {seconds:.1f}초 cache_hits={emb.get('cache_hits')} "
        f"cache_misses={emb.get('cache_misses')} cost_usd={emb.get('cost_usd')}",
        file=sys.stderr,
    )


@dataclass
class _EmbeddingStatsOutput:
    path: Path | None
    embedder: CachingEmbedder | None = None


def _write_embedding_stats(path: Path, embedder: CachingEmbedder) -> None:
    """같은 폴더의 임시 파일을 완성한 뒤 exclusive hard link로 원자 게시한다."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(embedder.stats(), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # 같은 파일시스템에서 완성본만 게시하며 기존 파일은 덮지 않는다.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _run(args: argparse.Namespace) -> int:
    requested = getattr(args, "embedding_stats_out", None)
    path = Path(requested) if requested is not None else None
    if path is not None:
        if args.index != "on" or args.index_dry_run:
            print("--embedding-stats-out은 --index on의 실제 실행에서만 쓴다", file=sys.stderr)
            return 2
        out = Path(args.out)
        sink = out.with_name(out.stem + ".estimate.jsonl") if args.estimate is not None else out
        if path.exists() or path.is_symlink() or path.resolve() in {out.resolve(), sink.resolve()}:
            print("이미 있는 파일이나 평가 결과 경로에 임베딩 통계를 쓰지 않는다", file=sys.stderr)
            return 2
        path.parent.mkdir(parents=True, exist_ok=True)  # 쓰기 불가 경로는 유료 호출 전에 거절한다.
        with tempfile.TemporaryFile(dir=path.parent):
            pass  # mkdir(exist_ok)만으로 알 수 없는 부모의 실제 파일 생성 권한도 먼저 확인한다.
    observation = _EmbeddingStatsOutput(path)
    result: int | None = None
    try:
        result = _run_evaluation(args, observation)
        return result
    finally:
        original_error = sys.exception()
        if observation.path is not None and observation.embedder is not None:
            try:
                _write_embedding_stats(observation.path, observation.embedder)
            except Exception as exc:
                if original_error is None and result == 0:
                    raise
                # 관측 저장 실패로 원래 예외·종료 코드를 덮지 않는다. 원문은 출력하지 않는다.
                print(f"임베딩 통계 저장 실패: {type(exc).__name__} (비용 미관측)", file=sys.stderr)


def _run_evaluation(args: argparse.Namespace, observation: _EmbeddingStatsOutput) -> int:
    if args.split == "test" and not args.allow_test:
        raise SealedSplitError("test 분할 봉인")  # 출력 파일을 만들기 전에 거부한다
    dataset = load_dataset(args.dataset)
    smoke_excluded: int | None = None
    if args.subset == "smoke":
        if args.split != "dev":
            print("--subset smoke는 dev 분할에서만 쓴다", file=sys.stderr)
            return 2
        excluded = _review_excluded(args.dataset)
        listed = set(dataset.manifest.get("smoke_task_ids") or [])
        smoke_excluded = len(listed & excluded - {t.task_id for t in dataset.tasks})
        dataset = _smoke_subset(dataset, excluded)
        if smoke_excluded:
            print(f"스모크 목록에서 검수 제외 과제 {smoke_excluded}개를 건너뛴다", file=sys.stderr)
    out = Path(args.out)
    sink_path = out.with_name(out.stem + ".estimate.jsonl") if args.estimate is not None else out
    if sink_path.exists():
        print(f"이미 있는 파일에는 쓰지 않는다: {sink_path}", file=sys.stderr)
        return 2
    if args.runflow_variant and args.sut != "runflow_v0":
        print("--runflow-variant는 --sut runflow_v0에서만 쓴다", file=sys.stderr)
        return 2
    if args.pattern is not None and args.sut != "runflow_v0":
        print("--pattern은 --sut runflow_v0에서만 쓴다", file=sys.stderr)
        return 2
    if (args.index == "on" or args.index_dry_run) and args.sut != "runflow_v0":
        print("--index on은 --sut runflow_v0에서만 쓴다", file=sys.stderr)
        return 2
    if args.index_dry_run and args.index != "on":
        print("--index-dry-run은 --index on과 함께 쓴다", file=sys.stderr)
        return 2
    if args.index == "off" and args.max_embed_cost_usd is not None:
        print("--max-embed-cost-usd는 --index on에서만 쓴다", file=sys.stderr)
        return 2
    if args.live_llm and args.llm_script:
        print("--live-llm과 --llm-script는 함께 쓸 수 없다", file=sys.stderr)
        return 2
    index_setup: _IndexSetup | None = None
    if args.index == "on":
        index_setup = _index_setup(args, dataset)
        if index_setup is None:
            return 2
        if args.index_dry_run:
            print(json.dumps(index_setup.dry_run(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
    llm_config: dict[str, Any] | None = None
    model_id: str = args.model_id
    if args.live_llm:
        live = _live_llm(args.model_id)
        if live is None:
            return 2
        llm_factory, llm_config, model_id = live
    else:
        llm_factory = load_script(args.llm_script) if args.llm_script else scripted_llm_factory({})
    database_env = _database_env(args.sut)
    if args.sut in DATABASE_SUTS and database_env is None:
        return 2  # 출력 파일을 만들기 전에 거부한다
    sut_options: dict[str, Any] = {"index_mode": args.index} if args.sut in DATABASE_SUTS else {}
    if args.sut == "runflow_v0":
        sut_options["variants"] = tuple(args.runflow_variant)
        if args.pattern is not None:
            sut_options["pattern"] = args.pattern
    provider: EvalIndexProvider | None = None
    if (
        index_setup is not None
    ):  # 출력 파일을 만들기 전에 색인을 준비한다(실패해도 파일이 남지 않는다)
        observation.embedder = index_setup.embedder
        provider, seconds = asyncio.run(index_setup.build())
        sut_options["index"] = provider
        _print_index_summary(provider, index_setup.embedder, seconds)
    sha, dirty = _git_state(out.parent)  # 출력 파일을 만들기 전에 읽는다
    common: dict[str, Any] = {
        "split": args.split,
        "sut_factory": lambda: create_sut(args.sut, **sut_options),
        "database_env": database_env,
        "llm_factory": llm_factory,
        "seed": args.seed,
        "budget": Budget(args.max_steps, args.max_episode_tokens, args.max_seconds),
        "experiment_budget": ExperimentBudget(args.max_cost_usd, args.max_total_tokens),
        "fault": _load_fault_plan(args.fault_plan),
        "model_id": model_id,
        "llm_config": llm_config,
        "smoke_excluded": smoke_excluded,
        "prompt_version": args.prompt_version,
        "git_sha": sha,
        "git_dirty": dirty,
        "experiment_id": args.experiment_id,
        "allow_sealed_test": args.allow_test,
    }
    sink_path.parent.mkdir(parents=True, exist_ok=True)  # 결과 폴더가 없으면 만든다
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
    if provider is not None and provider.zero_hit_searches:
        print(
            f"경고: 색인 메일이 있는데 search_indexed_mail이 0건을 돌려준 검색 "
            f"{provider.zero_hit_searches}회",
            file=sys.stderr,
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
        "answer_kind_rate": stats.answer_kind_rate(records),
        "unneeded_clarify_rate": stats.unneeded_clarify_rate(records),
        "state_diff_mismatch_episodes": stats.state_diff_mismatch_count(records),
        "write_tasks": stats.write_task_summary(records),
        "model_mismatch_episodes": stats.model_mismatch_count(records),
        "response_models": stats.response_model_set(records),
    }
    report.update(metrics_v1.extra_metrics(records))  # 보조 지표 섹션(ADR-0029 §2)
    schemas = sorted({int(r.get("result_schema", 1)) for r in records})
    if len(schemas) > 1:
        msg = f"결과 행의 result_schema가 섞여 있다({schemas}). 기록이 없는 행의 보조 지표는 빠진다"
        report.setdefault("warnings", []).append(msg)
        print(f"경고: {msg}", file=sys.stderr)
    by_reading = stats.ambiguity_ignored_by_reading(records)
    if by_reading is not None:
        report["ambiguity_ignored_by_reading"] = by_reading
    if not report["response_models"]["single"]:
        report.setdefault("warnings", []).append(
            "실행 전체의 응답 모델이 하나가 아니다. 결과를 같은 모델로 해석하지 않는다"
        )
        print("경고: 실행 전체의 응답 모델이 하나가 아니다", file=sys.stderr)
    if args.compare:
        other = stats.load_results(args.compare, reject_model_mismatch=reject)
        report["paired_difference_a_minus_b"] = stats.paired_difference(
            records, other, **kw
        ).__dict__
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _judge_input(args: argparse.Namespace) -> int:
    records = stats.load_results(args.results)
    out = Path(args.out)
    if out.exists():
        print(f"이미 있는 파일에는 쓰지 않는다: {out}", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with out.open("x", encoding="utf-8") as fh:
        for row in metrics_v1.judge_inputs(records):
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            fh.write("\n")
            rows += 1
    print(f"judge 입력 {rows}건을 {out}에 썼다")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "judge":
            return judge.run_cli(args)
        return _judge_input(args) if args.command == "judge-input" else _stats(args)
    except SealedSplitError:
        print(f"test 분할은 봉인돼 있다. 실행하려면 {SEAL_FLAG}를 명시한다.", file=sys.stderr)
        return 2
    except EmbedBudgetExceeded as exc:
        print(f"임베딩 비용 상한으로 중단했다: {exc}", file=sys.stderr)
        return 3
    except (
        AsOfBeforeIndexedMail,
        IndexBuildError,
        DatasetError,
        stats.ResultsError,
        UnknownSutError,
    ) as exc:
        # 이 예외들의 메시지는 위치·이름만 담도록 직접 만든 것이다.
        print(f"오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        # 외부 라이브러리 오류(pydantic 등)의 메시지에는 입력 원문이 들어갈 수 있어 이름만 쓴다.
        print(f"오류: {type(exc).__name__}", file=sys.stderr)
        return 2
