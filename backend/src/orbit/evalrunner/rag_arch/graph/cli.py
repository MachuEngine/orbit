"""`python -m orbit.evalrunner.rag_arch.graph build|dry-run|quality` (평가 전용, ADR-0036 A4).

- dry-run: LLM·DB 호출 없이 호출 수와 비용을 추정한다.
- build: 색인(없으면 만든다)을 준비한 뒤 계정마다 그래프를 구축한다. 실제 LLM이 필요하다
  (`--live-llm`). 이미 끝난 그래프는 재사용하고, 중단된 구축은 이어서 한다.
- quality: 구축된 그래프를 정답(`graph_gold.json`)과 비교한다(DB 읽기만).
"""

import argparse
import asyncio
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock, SystemClock
from orbit.core.config import get_llm_settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.evalrunner.rag_arch.graph import build as gb
from orbit.evalrunner.rag_arch.graph import quality as gq
from orbit.evalrunner.rag_arch.graph import store
from orbit.evalrunner.rag_arch.graph.prompts import ENTITY_KINDS
from orbit.evalrunner.retrieval import worldmail
from orbit.evalrunner.retrieval.embed_cache import CachingEmbedder
from orbit.evalrunner.runflow_index import build_eval_indexes
from orbit.evalrunner.task_source import DatasetError, load_dataset
from orbit.llm.factory import build_llm, llm_config_info

GOLD_FILE = "graph_gold.json"


def _positive_finite(raw: str) -> float:
    """비용 상한은 유한하고 0보다 큰 값만 받는다(nan·inf·0·음수는 사용법 오류)."""
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"숫자가 아니다: {raw}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"0보다 큰 유한한 값이어야 한다: {raw}")
    return value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orbit.evalrunner.rag_arch.graph")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("build", "dry-run", "quality"):
        c = sub.add_parser(name)
        c.add_argument("--dataset", required=True, help="예: eval/datasets/orbit-eval/rag-v1")
        c.add_argument("--accounts", nargs="*", default=None, help="기본: 과제가 있는 모든 계정")
        c.add_argument("--embedder", choices=["openai", "fake"], default="openai")
        c.add_argument("--embed-cache-dir", default=None)
        c.add_argument("--price-per-mtok", type=float, default=None, help="임베딩 1M 토큰당 달러")
        c.add_argument("--max-embed-cost-usd", type=float, default=None)
        c.add_argument("--out", default=None, help="결과 JSON 경로(없으면 표준 출력만)")
    sub.choices["build"].add_argument("--live-llm", action="store_true")
    sub.choices["build"].add_argument("--model-id", default="unspecified")
    sub.choices["build"].add_argument("--max-cost-usd", type=_positive_finite, default=1.0)
    sub.choices["build"].add_argument("--rebuild", action="store_true")
    return p


def _emit(report: dict[str, Any], out: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as fh:  # 기존 파일은 덮지 않는다
            fh.write(text + "\n")


def _gold(dataset_dir: str, accounts: list[str]) -> dict[str, list[dict[str, Any]]]:
    data = json.loads((Path(dataset_dir) / GOLD_FILE).read_text(encoding="utf-8"))
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in data["triples"]:
        if t["account_id"] in accounts:
            by_account[t["account_id"]].append(t)
    return by_account


def _gold_edges(triples: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (gq.norm(t["subject"]), gq.norm(t["object"]))
        for t in triples
        if t["object_type"] in ENTITY_KINDS
    ]


def _dry_run(args: argparse.Namespace) -> int:
    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    accounts = args.accounts or sorted({t.account_id for t in dataset.tasks})
    world = SimWorld.load(dict(dataset.world_snapshot), FixedClock(worldmail.INDEX_NOW))
    messages = [m for a in accounts for m in worldmail.mailbox_messages(world, a)]
    gold = [t for ts in _gold(args.dataset, accounts).values() for t in ts]
    price = gb.PriceSettings()
    report = gb.dry_run_estimate(messages, _gold_edges(gold), price)
    report["prices_per_mtok"] = {
        "input": price.llm_price_input_per_mtok,
        "output": price.llm_price_output_per_mtok,
    }
    report["accounts"] = accounts
    _emit(report, args.out)
    return 0


def _prepare(args: argparse.Namespace) -> tuple[Any, ...] | None:
    """(데이터셋, 계정, 임베더, URL) 또는 오류 메시지를 낸 뒤 None."""
    from orbit.evalrunner.retrieval import cli as retrieval_cli  # noqa: PLC0415  순환 방지

    try:
        dataset = load_dataset(args.dataset)
        accounts = args.accounts or sorted({t.account_id for t in dataset.tasks})
        model_id, fake, settings = retrieval_cli._embedder_parts(args.embedder)
        urls = retrieval_cli._eval_urls()
        inner = fake if fake is not None else retrieval_cli._build_openai(settings)
    except (DatasetError, retrieval_cli.UsageError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return None
    cache_dir = Path(args.embed_cache_dir or retrieval_cli.DEFAULT_CACHE_DIR)
    embedder = CachingEmbedder(
        inner, cache_dir, max_cost_usd=args.max_embed_cost_usd, price_per_mtok=args.price_per_mtok
    )
    return dataset, accounts, embedder, urls, model_id


async def _build(args: argparse.Namespace, prepared: tuple[Any, ...]) -> int:
    dataset, accounts, embedder, urls, _ = prepared
    if not args.live_llm:
        print("build에는 --live-llm이 필요하다", file=sys.stderr)
        return 2
    try:
        settings = get_llm_settings()
    except ValidationError as exc:
        names = sorted({".".join(str(p) for p in e["loc"]) or "(설정 전체)" for e in exc.errors()})
        print(f"LLM 설정 오류 (확인할 항목: {', '.join(names)})", file=sys.stderr)
        return 2
    if args.model_id not in ("unspecified", settings.llm_model_id):
        print("--model-id가 ORBIT_LLM_MODEL_ID와 다르다", file=sys.stderr)
        return 2
    llm = build_llm(settings)
    price = gb.PriceSettings()
    world = SimWorld.load(dict(dataset.world_snapshot), FixedClock(worldmail.INDEX_NOW))
    provider, _ = await build_eval_indexes(
        world_snapshot=dataset.world_snapshot,
        world_sha256=worldmail.file_sha256(Path(args.dataset) / "world.json"),
        accounts=accounts,
        app_url=urls[0],
        owner_url=urls[1],
        embedder=embedder,
    )
    db = AsyncDatabase.from_url(urls[0], pool_size=2, max_overflow=2)
    reports: dict[str, Any] = {}
    exit_code = 0
    try:
        for account in accounts:
            # 상한은 선택한 모든 계정의 DB 누적 비용 + 이번 실행 지출의 합에 건다. 이 계정의 누적
            # 비용은 build_graph가 직접 읽으므로 여기서는 다른 계정 몫만 넘긴다.
            others = [provider.handles[a].workspace_id for a in accounts if a != account]
            other_spent, other_unknown = await gb.recorded_cost(db, others)
            if other_unknown:
                reports[account] = {
                    "stopped": "다른 계정에 비용을 모르는 호출이 있어 상한을 지킬 수 없다"
                }
                exit_code = 3
                break
            try:
                report = await gb.build_graph(
                    db=db,
                    ws=provider.handles[account].workspace_id,
                    messages=worldmail.mailbox_messages(world, account),
                    llm=llm,
                    embedder=embedder,
                    ids=Uuid7Generator(),
                    clock=SystemClock(),
                    model_id=settings.llm_model_id,
                    price=price,
                    max_cost_usd=args.max_cost_usd,
                    other_spent_usd=other_spent,
                    rebuild=args.rebuild,
                )
            except gb.BudgetStop as exc:
                reports[account] = {"stopped": str(exc)}
                exit_code = 3
                break
            except (gb.GraphConfigConflict, gb.IndexMismatch) as exc:
                reports[account] = {"error": str(exc)}
                exit_code = 2
                break
            reports[account] = report.to_dict()
    finally:
        await db.dispose()
    _emit({"llm": llm_config_info(settings), "accounts": reports}, args.out)
    return exit_code


async def _quality(args: argparse.Namespace, prepared: tuple[Any, ...]) -> int:
    dataset, accounts, embedder, urls, _ = prepared
    provider, _ = await build_eval_indexes(
        world_snapshot=dataset.world_snapshot,
        world_sha256=worldmail.file_sha256(Path(args.dataset) / "world.json"),
        accounts=accounts,
        app_url=urls[0],
        owner_url=urls[1],
        embedder=embedder,
    )
    world = SimWorld.load(dict(dataset.world_snapshot), FixedClock(worldmail.INDEX_NOW))
    gold = _gold(args.dataset, accounts)
    db = AsyncDatabase.from_url(urls[0], pool_size=2, max_overflow=2)
    out: dict[str, Any] = {}
    try:
        for account in accounts:
            ws = provider.handles[account].workspace_id
            async with db.workspace_session(ws) as s:
                ents = await store.entity_mails(s, ws)
                rels = await store.all_relations(s, ws)
                raw = await store.cost_rows(s, ws)
            out[account] = gq.evaluate_quality(
                gold.get(account, []),
                [gq.FoundEntity(n, k, m) for n, k, m in ents],
                [
                    gq.FoundRelation(
                        r.provider_item_id,
                        r.subject_name,
                        r.predicate,
                        r.object_name or "",
                        r.object_id is None,
                    )
                    for r in rels
                ],
                mails_total=len(worldmail.mailbox_messages(world, account)),
                format_errors=sum(1 for e in raw["extractions"] if e.status == "format_error"),
            )
    finally:
        await db.dispose()
    _emit(out, args.out)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "dry-run":
        return _dry_run(args)
    prepared = _prepare(args)
    if prepared is None:
        return 2
    runner = _build if args.cmd == "build" else _quality
    return asyncio.run(runner(args, prepared))
