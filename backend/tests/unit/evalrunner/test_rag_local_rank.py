"""A4R·A4RN 순위·지문 단위 테스트(ADR-0037). 데이터는 모두 합성 값이다."""

import uuid
from typing import Any

import pytest

from orbit.evalrunner.rag_arch import local_rank as lr
from orbit.evalrunner.rag_arch.config import (
    ARCHS,
    GRAPH_RERANK_CANDIDATES,
    LOCAL_RANK_VERSION,
    MAIN_ARCHS,
    R03B_ARCHS,
)
from orbit.evalrunner.rag_arch.graph import query as gqr
from orbit.evalrunner.rag_arch.graph import store
from orbit.evalrunner.rag_arch.graph.prompts import LOCAL_MAX_MAILS
from orbit.evalrunner.rag_arch.sut import RagArchSUT


def test_new_archs_are_selectable_but_not_in_default_run() -> None:
    assert R03B_ARCHS == ("A4R", "A4RN")
    assert set(R03B_ARCHS) <= set(ARCHS) and not set(R03B_ARCHS) & set(MAIN_ARCHS)


def test_first_stage_order_by_distance_then_id_with_chunkless_last() -> None:
    dist = {"m3": 0.2, "m1": 0.2, "m2": 0.1, "m9": 0.5}
    assert lr.first_stage_order(["m9", "m3", "m1", "m2", "mz", "ma"], dist) == [
        "m2", "m1", "m3", "m9", "ma", "mz",
    ]  # fmt: skip
    assert lr.first_stage_order(None, dist) == ["m2", "m1", "m3", "m9"]


@pytest.mark.parametrize("count", [GRAPH_RERANK_CANDIDATES - 5, GRAPH_RERANK_CANDIDATES + 25])
async def test_first_stage_keeps_at_most_candidates_and_reports_all(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [f"m{i:03}" for i in range(count)]

    async def fake(*_a: Any, **_k: Any) -> dict[str, float]:
        return {m: float(count - i) for i, m in enumerate(ids)}  # 뒤쪽 메일이 더 가깝다

    monkeypatch.setattr(lr, "mail_distances", fake)
    head, total = await lr.first_stage(None, uuid.uuid4(), [0.0], ids)  # type: ignore[arg-type]
    assert total == count and len(head) == min(count, GRAPH_RERANK_CANDIDATES)
    assert head[0] == ids[-1]


def test_second_stage_orders_by_score_ties_by_first_stage_and_caps() -> None:
    head = [f"m{i}" for i in range(20)]
    scores = [0.0] * 20
    scores[7], scores[3], scores[15] = 0.9, 0.5, 0.5
    out = lr.second_stage(head, scores)
    assert out[:3] == ["m7", "m3", "m15"] and out[3:5] == ["m0", "m1"]  # 동점은 1단계 순서
    assert len(out) == LOCAL_MAX_MAILS
    assert lr.second_stage(head[:3], [0.1, 0.3, 0.2]) == ["m1", "m2", "m0"]  # 12통 미만
    assert lr.second_stage([], []) == []
    with pytest.raises(RuntimeError, match="점수 개수"):
        lr.second_stage(head, scores[:-1])


def rel(mail: str, rid: int) -> store.RelationRow:
    u = uuid.UUID(int=rid)
    return store.RelationRow(u, u, "s", "p", None, "o", "o", u, mail)


def test_local_plan_keeps_uncut_list_and_a4_ranking_unchanged() -> None:
    hops = [(1, rel(f"m{i:03}", i)) for i in range(LOCAL_MAX_MAILS + 5)]
    assert gqr.rank_mails(hops) == gqr.rank_all_mails(hops)[:LOCAL_MAX_MAILS]
    assert len(gqr.rank_all_mails(hops)) == LOCAL_MAX_MAILS + 5


def test_relation_lines_follow_the_new_mail_order() -> None:
    hops = [(1, rel("m1", 1)), (1, rel("m2", 2))]
    old = gqr.LocalPlan(["m1", "m2"], hops, 1, 1, 0, ["m1", "m2"])
    new = gqr.LocalPlan(["m2", "m1"], hops, 1, 1, 0, ["m1", "m2"])
    handles = {"m1": "M1", "m2": "M2"}
    assert [x[:4] for x in gqr.relation_lines(old, handles)] == ["[M1]", "[M2]"]
    assert [x[:4] for x in gqr.relation_lines(new, handles)] == ["[M2]", "[M1]"]


class _Index:
    def config_info(self) -> dict[str, Any]:
        return {"stub": True}


class _Reranker:
    model_id = "fake/r"
    revision = "abc"
    max_length = 768
    device = "cpu"

    def score(self, query: str, passages: Any) -> list[float]:
        return []


def make(arch: str, **kw: Any) -> RagArchSUT:
    return RagArchSUT(
        rag_arch=arch,
        index=_Index(),  # type: ignore[arg-type]
        reranker=_Reranker(),
        **kw,
    )


def test_a4r_and_a4rn_fingerprints() -> None:
    with pytest.raises(ValueError, match="graph_digests"):
        make("A4R")
    a4 = make("A4", graph_digests={"a": "d"})
    a4r = make("A4R", graph_digests={"a": "d"})
    rn = make("A4RN")
    assert LOCAL_RANK_VERSION in a4r.version and LOCAL_RANK_VERSION not in a4.version
    assert a4r.version != a4.version and LOCAL_RANK_VERSION in rn.version
    i4, ir, irn = (x.config_info(model_id="m") for x in (a4, a4r, rn))
    for info in (ir, irn):
        assert info["local_rank_version"] == LOCAL_RANK_VERSION
        assert info["graph_rerank_candidates"] == GRAPH_RERANK_CANDIDATES
        assert info["local_max_mails"] == LOCAL_MAX_MAILS
        assert info["reranker"] == {
            "model_id": "fake/r", "revision": "abc", "max_length": 768, "device": "cpu",
        }  # fmt: skip
    assert "local_rank_version" not in i4 and "reranker" not in i4
    assert ir["graph_digests"] == {"a": "d"} and ir["hops"] == i4["hops"]
    assert irn["graph"] == "none" and "graph_digests" not in irn and "hops" not in irn
    assert len({i4["condition_digest"], ir["condition_digest"], irn["condition_digest"]}) == 3
