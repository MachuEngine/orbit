"""평가 질의 선택 (ADR-0028 §4·§5). 검수 제외를 반영하고, 등급 2 정답이 메일인 질의만 남긴다."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orbit.connectors.sim.world import SimWorld
from orbit.evaldata.v1.search_set import load_search_set

SEARCH_FILE = "search.jsonl"
SEARCH_MANIFEST = "search-manifest.json"


@dataclass(frozen=True)
class MailQuery:
    query_id: str
    query: str
    form: str
    cluster_id: str
    # 크기("200" 등) -> (계정 ID, 메일 정답 {메일 ID: 등급 0~2})
    accounts: dict[str, str]
    relevance: dict[str, dict[str, int]]


@dataclass(frozen=True)
class QuerySelection:
    queries: tuple[MailQuery, ...]
    counts: dict[str, Any]


def read_rows(dataset_dir: Path) -> list[dict[str, Any]]:
    """검수 제외를 반영한 검색 세트(전체 분할)."""
    return load_search_set(
        dataset_dir / SEARCH_FILE, dataset_dir / SEARCH_MANIFEST, include_excluded=False
    )


def search_sha256(dataset_dir: Path) -> str:
    return hashlib.sha256((dataset_dir / SEARCH_FILE).read_bytes()).hexdigest()


def select_queries(
    dataset_dir: Path, split: str, world: SimWorld, sizes: tuple[str, ...]
) -> QuerySelection:
    """`split`의 질의 중 모든 `sizes` 메일함에서 등급 2 정답이 메일인 질의.

    메일 정답은 정답 ID가 해당 계정의 메일로 존재하는 항목이다(문서 ID는 빠진다). 등급 2 메일
    정답이 없는 질의(정답이 문서)는 빼고 수를 `counts`에 남긴다.
    """
    all_rows = load_search_set(
        dataset_dir / SEARCH_FILE, dataset_dir / SEARCH_MANIFEST, include_excluded=True
    )
    kept = {r["query_id"] for r in read_rows(dataset_dir)}
    in_split = [r for r in all_rows if r["split"] == split]
    review_excluded = [r for r in in_split if r["query_id"] not in kept]
    candidates = [r for r in in_split if r["query_id"] in kept]
    selected: list[MailQuery] = []
    document_answer: list[str] = []
    for row in candidates:
        accounts: dict[str, str] = {}
        relevance: dict[str, dict[str, int]] = {}
        has_mail_answer = True
        for size in sizes:
            mailbox = row["mailboxes"][size]
            account_id = str(mailbox["account_id"])
            known = world.account(account_id).messages
            mail_rel = {k: int(g) for k, g in mailbox["relevance"].items() if k in known}
            accounts[size] = account_id
            relevance[size] = mail_rel
            if 2 not in mail_rel.values():
                has_mail_answer = False
        if has_mail_answer:
            selected.append(
                MailQuery(
                    query_id=row["query_id"],
                    query=row["query"],
                    form=row["form"],
                    cluster_id=row["cluster_id"],
                    accounts=accounts,
                    relevance=relevance,
                )
            )
        else:
            document_answer.append(row["query_id"])
    counts = {
        "split": split,
        "in_split_before_review": len(in_split),
        "review_excluded": len(review_excluded),
        "document_answer_excluded": len(document_answer),
        "document_answer_query_ids": sorted(document_answer),
        "selected": len(selected),
        "clusters": len({q.cluster_id for q in selected}),
    }
    return QuerySelection(tuple(selected), counts)
