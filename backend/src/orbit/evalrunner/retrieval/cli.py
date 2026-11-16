"""검색 평가 실행기 CLI (ADR-0028).

    python -m orbit.evalrunner.retrieval run --dataset <dir> --split dev --methods all \
        --sizes 200,1000,5000 --out runs/retrieval/<id>.json [--dry-run] [--max-cost-usd X] \
        [--embedder openai|fake]

종료 코드: 0 정상, 2 사용 오류(봉인된 test 분할, 파일 충돌, 잘못된 입력, 설정 없음),
3 비용 상한으로 중단.
`--dry-run`은 임베딩 호출·DB 접근 없이 추정만 출력한다. 평가 DB(`orbit_eval`)만 쓴다.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from orbit.db.session import AsyncDatabase
from orbit.evaldata.v1 import search_set
from orbit.evalrunner import stats
from orbit.evalrunner.cli import SEAL_FLAG, _git_state
from orbit.evalrunner.retrieval import queries as qmod
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.dryrun import dry_run_report
from orbit.evalrunner.retrieval.embed_cache import BudgetExceeded, CachingEmbedder
from orbit.evalrunner.retrieval.indexer import IndexBuildError
from orbit.evalrunner.retrieval.methods import parse_methods
from orbit.evalrunner.retrieval.runner import (
    RunSettings,
    run_retrieval,
    sequence_sizes,
    write_atomic,
)
from orbit.evalrunner.runflow_env import EvalDatabaseSettings
from orbit.evalrunner.runner import SealedSplitError
from orbit.mailindex.embedding import Embedder, FakeEmbedder, OpenAIEmbedder
from orbit.mailindex.limits import DEFAULT_EMBEDDING_MODEL_ID

REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_CACHE_DIR = REPO_ROOT / "runs" / "embed-cache"
TEST_LEDGER = "test-runs.jsonl"


class UsageError(Exception):
    """사용자에게 보여 줄 메시지만 담는다(비밀 값 없음)."""


class EmbedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORBIT_", extra="ignore")

    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orbit.evalrunner.retrieval")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="검색 평가를 실행한다")
    run.add_argument("--dataset", required=True)
    run.add_argument("--split", required=True, choices=["dev", "test"])
    run.add_argument(
        "--methods", default="all", help="all 또는 쉼표 목록(gmail_search,keyword,...)"
    )
    run.add_argument("--sizes", default="200,1000,5000")
    run.add_argument("--out", default=None)
    run.add_argument("--dry-run", action="store_true", help="임베딩 호출 없이 비용만 추정한다")
    run.add_argument("--max-cost-usd", type=float, default=None, help="임베딩 누적 비용 상한")
    run.add_argument("--embedder", choices=["openai", "fake"], default="openai")
    run.add_argument("--price-per-mtok", type=float, default=None, help="1M 토큰당 달러(재정의)")
    run.add_argument("--embed-cache-dir", default=str(DEFAULT_CACHE_DIR))
    run.add_argument("--experiment-id", default=None)
    run.add_argument("--resamples", type=int, default=stats.DEFAULT_RESAMPLES)
    run.add_argument("--seed", type=int, default=stats.DEFAULT_SEED, help="bootstrap 시드")
    run.add_argument(SEAL_FLAG, dest="allow_test", action="store_true")
    return p


def _embedder_parts(name: str) -> tuple[str, Embedder | None, EmbedSettings]:
    """(모델 ID, 임베더(필요할 때만), 설정). openai 임베더는 키가 있을 때만 만든다."""
    settings = EmbedSettings()
    if name == "fake":
        fake = FakeEmbedder()
        return fake.model_id, fake, settings
    return settings.embedding_model_id, None, settings


def _build_openai(settings: EmbedSettings) -> Embedder:
    if settings.openai_api_key is None:
        raise UsageError("openai 임베딩에는 ORBIT_OPENAI_API_KEY가 필요하다")
    return OpenAIEmbedder(
        api_key=settings.openai_api_key,
        model_id=settings.embedding_model_id,
        base_url=settings.openai_base_url,
    )


def _eval_urls() -> tuple[str, str]:
    """(앱 역할 URL, 소유자 역할 URL). 둘 다 개발 DB(orbit)면 거부한다."""
    try:
        app_url = EvalDatabaseSettings().eval_database_url.get_secret_value()
    except ValidationError:
        raise UsageError("ORBIT_EVAL_DATABASE_URL(평가 DB, 앱 역할)이 필요하다") from None
    owner_url = os.environ.get("ORBIT_EVAL_MIGRATION_DATABASE_URL", "")
    if not owner_url:
        raise UsageError(
            "ORBIT_EVAL_MIGRATION_DATABASE_URL(평가 DB, 소유자 역할)이 필요하다"
            "(합성 연결 행을 만들 때만 쓴다)"
        )
    for url in (app_url, owner_url):
        if (make_url(url).database or "") != "orbit_eval":
            raise UsageError("검색 평가는 전용 DB(orbit_eval)에서만 실행한다")
    return app_url, owner_url


def _accounts(rows: list[dict[str, Any]], sizes: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for size in sizes:
        found = {str(r["mailboxes"][size]["account_id"]) for r in rows}
        if len(found) != 1:
            raise UsageError(f"메일함 크기 {size}의 계정이 하나가 아니다")
        out[size] = found.pop()
    return out


def _record_test_run(out_dir: Path, experiment: str | None) -> int:
    """test 실행 횟수를 장부에 한 줄 덧붙이고, 이번을 포함한 횟수를 돌려준다(ADR-0010)."""
    ledger = out_dir / TEST_LEDGER
    out_dir.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"experiment_id": experiment}) + "\n")
    return len([line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()])


def _run(args: argparse.Namespace) -> int:
    if args.split == "test" and not args.allow_test:
        raise SealedSplitError("test 분할 봉인")  # 출력 파일·DB를 만들기 전에 거부한다
    sizes = sequence_sizes(args.sizes)
    methods = parse_methods(args.methods)
    dataset = Path(args.dataset)
    out = Path(args.out) if args.out else None
    if not args.dry_run:
        if out is None:
            raise UsageError("--out이 필요하다(--dry-run이 아니면)")
        if out.exists():
            raise UsageError(f"이미 있는 파일에는 쓰지 않는다: {out}")
    model_id, fake, embed_settings = _embedder_parts(args.embedder)
    cache_dir = Path(args.embed_cache_dir)
    world_path = dataset / "world.json"
    world = worldmail.load_world(world_path)
    rows = qmod.read_rows(dataset)
    accounts = _accounts(rows, sizes)
    selection = qmod.select_queries(dataset, args.split, world, sizes)

    if args.dry_run:
        report = dry_run_report(
            world=world,
            sizes=sizes,
            accounts=accounts,
            query_texts=[q.query for q in selection.queries],
            model_id=model_id,
            cache_dir=cache_dir,
            price_per_mtok=args.price_per_mtok,
            needs_query_embedding=bool({"vector", "hybrid"} & set(methods)),
        )
        report["queries_selected"] = selection.counts
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    assert out is not None  # noqa: S101  위에서 확인했다
    app_url, owner_url = _eval_urls()
    inner = fake if fake is not None else _build_openai(embed_settings)
    embedder = CachingEmbedder(
        inner, cache_dir, max_cost_usd=args.max_cost_usd, price_per_mtok=args.price_per_mtok
    )
    sha, dirty = _git_state(out.parent)  # 출력 파일을 만들기 전에 읽는다
    test_runs = _record_test_run(out.parent, args.experiment_id) if args.split == "test" else None
    settings = RunSettings(
        split=args.split,
        sizes=sizes,
        methods=methods,
        resamples=args.resamples,
        bootstrap_seed=args.seed,
        experiment_id=args.experiment_id,
        dataset=dataset.name,
        world_sha256=worldmail.file_sha256(world_path),
        search_sha256=qmod.search_sha256(dataset),
        git_sha=sha,
        git_dirty=dirty,
        test_runs=test_runs,
    )
    result = asyncio.run(
        _execute(settings, selection, world, app_url, owner_url, embedder, accounts)
    )
    write_atomic(out, json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    emb = result["embedding"]
    print(
        f"experiment_id={result['experiment_id']} queries={result['queries']['selected']} "
        f"seconds={result['timing']['total_seconds']} cache_hits={emb['cache_hits']} "
        f"cache_misses={emb['cache_misses']} cost_usd={emb['cost_usd']}"
    )
    return 0


async def _execute(
    settings: RunSettings,
    selection: qmod.QuerySelection,
    world: Any,
    app_url: str,
    owner_url: str,
    embedder: CachingEmbedder,
    accounts: dict[str, str],
) -> dict[str, Any]:
    db = AsyncDatabase.from_url(app_url)
    try:
        return await run_retrieval(
            settings=settings,
            selection=selection,
            world=world,
            db=db,
            owner_url=owner_url,
            embedder=embedder,
            accounts=accounts,
        )
    finally:
        await db.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except SealedSplitError:
        print(f"test 분할은 봉인돼 있다. 실행하려면 {SEAL_FLAG}를 명시한다.", file=sys.stderr)
        return 2
    except UsageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except BudgetExceeded as exc:
        print(f"비용 상한으로 중단했다: {exc}", file=sys.stderr)
        return 3
    except (IndexBuildError, search_set.SearchSetError) as exc:
        print(f"오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        # 외부 라이브러리 오류의 메시지에는 입력 원문이 들어갈 수 있어 이름만 쓴다.
        print(f"오류: {type(exc).__name__}", file=sys.stderr)
        return 2
