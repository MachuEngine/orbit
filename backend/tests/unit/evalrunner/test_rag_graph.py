"""A4 GraphRAG 순수 로직 단위 테스트(ADR-0036). 데이터는 모두 합성 값이다."""

import json
import random
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from orbit.connectors.contracts import MailMessage
from orbit.evalrunner.rag_arch.config import ARCHS, prompt_sha
from orbit.evalrunner.rag_arch.graph import build as gb
from orbit.evalrunner.rag_arch.graph import quality as gq
from orbit.evalrunner.rag_arch.graph import query as gqr
from orbit.evalrunner.rag_arch.graph import store
from orbit.evalrunner.rag_arch.graph.extract import parse_extraction, render_mail
from orbit.evalrunner.rag_arch.graph.names import name_key, squash
from orbit.evalrunner.rag_arch.graph.prompts import (
    EXTRACT_PROMPT,
    LOCAL_MAX_MAILS,
    ROUTE_PROMPT,
    SUMMARY_PROMPT,
)
from orbit.evalrunner.rag_arch.sut import RagArchSUT
from orbit.llm.scripted import ScriptedLLM
from orbit.llm.types import LLMResponse, LLMUsage

GOOD = json.dumps(
    {
        "entities": [
            {"name": "김민수님", "type": "person"},
            {"name": "가나 컴퍼니", "type": "company"},
            {"name": "엉터리", "type": "alien"},
        ],
        "relations": [
            {"subject": "김민수", "predicate": "works_at", "object": "가나컴퍼니"},
            {"subject": "가나컴퍼니", "predicate": "deadline", "object": "2026-12-01"},
            {"subject": "모르는이", "predicate": "works_at", "object": "가나컴퍼니"},
        ],
    },
    ensure_ascii=False,
)


def response(text: str | None, *, finish: Any = "stop") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=(),
        finish_reason=finish,
        usage=LLMUsage(10, 5),
        cost_usd=None,
        latency_ms=1,
        model_id=None,
        provider=None,
    )


def test_name_merge_ignores_space_punctuation_case_and_one_honorific() -> None:
    assert squash(" Gana-Co. ") == "ganaco"
    assert name_key("김민수님", "person") == name_key("김 민수 씨", "person") == "김민수"
    assert name_key("김민수 팀장님", "person") == "김민수"
    assert name_key("가나 컴퍼니", "company") == "가나컴퍼니"
    assert name_key("김씨", "person") == "김씨"  # 떼고 남는 글자가 2자 미만이면 그대로
    assert name_key("대표님", "company") == "대표님"  # 회사 이름에는 호칭 제거를 하지 않는다


def test_parse_extraction_merges_entities_keeps_literals_and_drops_unresolvable() -> None:
    result = parse_extraction(GOOD)
    assert result is not None
    assert set(result.entities) == {"김민수", "가나컴퍼니"}  # 알 수 없는 종류는 버린다
    by_pred = {r.predicate: r for r in result.relations}
    assert by_pred["works_at"].object_key == "가나컴퍼니"  # 공백·호칭이 달라도 같은 개체
    assert by_pred["deadline"].object_key is None and by_pred["deadline"].object_value
    assert result.dropped == 1  # 주어를 해석하지 못한 관계


@pytest.mark.parametrize(
    "text", [None, "", "not json", "[]", '{"entities":1,"relations":[]}', '{"entities":[]}']
)
def test_parse_extraction_format_failure_is_none(text: str | None) -> None:
    assert parse_extraction(text) is None


def mk_mail(mail_id: str = "msg-1", body: str = "본문") -> MailMessage:
    return MailMessage.model_validate(
        {
            "id": mail_id,
            "thread_id": "t-1",
            "from": {"email": "a@example.org", "name": "합성 발신자"},
            "to": [{"email": "b@example.org"}],
            "subject": "제목",
            "body_text": body,
            "sent_at": datetime(2026, 11, 5, tzinfo=UTC),
        }
    )


def test_render_mail_has_names_not_ids() -> None:
    mail = mk_mail()
    text = render_mail(mail)
    assert "합성 발신자" in text and "msg-1" not in text


def test_louvain_is_deterministic_and_independent_of_input_order() -> None:
    pytest.importorskip("networkx")
    left = ["a1", "a2", "a3", "a4"]
    right = ["b1", "b2", "b3", "b4"]
    edges = [(x, y) for g in (left, right) for i, x in enumerate(g) for y in g[i + 1 :]]
    edges.append(("a1", "b1"))
    keys = left + right + ["lonely"]
    first = gb.detect_communities(keys, edges)
    assert first == gb.detect_communities(keys, edges)
    shuffled_keys, shuffled_edges = keys[:], edges[:]
    rng = random.Random(1)  # noqa: S311  테스트용 고정 시드
    rng.shuffle(shuffled_keys)
    rng.shuffle(shuffled_edges)
    assert gb.detect_communities(shuffled_keys, [(b, a) for a, b in shuffled_edges]) == first
    assert tuple(left) in first and tuple(right) in first and ("lonely",) in first


def test_community_key_ignores_order_and_changes_with_members() -> None:
    assert gb.community_key(["b", "a"]) == gb.community_key(["a", "b"])
    assert gb.community_key(["a", "b"]) != gb.community_key(["a", "c"])


async def test_route_question_parses_and_falls_back() -> None:
    good = ScriptedLLM([response('{"mode":"global"}')])
    assert await gqr.route_question(good, "전체 현황은?", "t") == ("global", False)
    assert [m.content for m in good.requests[0].messages][1] == "전체 현황은?"  # 질문만 본다
    for bad in (response("글쎄"), response('{"mode":"x"}'), response("{}", finish="length")):
        assert await gqr.route_question(ScriptedLLM([bad]), "q", "t") == ("local", True)


def rel(mail: str, rid: int = 0) -> store.RelationRow:
    u = uuid.UUID(int=rid)
    return store.RelationRow(u, u, "s", "p", None, "o", "o", u, mail)


def test_rank_mails_orders_by_hop_then_relation_count_then_id_and_caps() -> None:
    hops = [(2, rel("m1", 1)), (1, rel("m3", 2)), (1, rel("m2", 3)), (1, rel("m2", 4))]
    assert gqr.rank_mails(hops) == ["m2", "m3", "m1"]
    many = [(1, rel(f"m{i:03}", i)) for i in range(LOCAL_MAX_MAILS + 5)]
    assert len(gqr.rank_mails(many)) == LOCAL_MAX_MAILS


def test_relation_lines_only_for_mails_with_handles() -> None:
    plan = gqr.LocalPlan(["m1"], [(1, rel("m1", 1)), (1, rel("m1", 1)), (2, rel("m9", 2))], 1, 1, 0)
    assert gqr.relation_lines(plan, {"m1": "M1"}) == ["[M1] s | p | o"]


def test_quality_matching_rules() -> None:
    assert gq.keys_match(gq.norm("김민수"), gq.norm("김민수님"))  # 호칭만 다르다
    assert not gq.keys_match("", "") and not gq.keys_match("김민수", "이영희")
    assert gq.values_match("2026-11-24", "2026.11.24") and gq.values_match(
        "22,530,000원", "22530000"
    )
    assert not gq.values_match("2026-11-24", "11/2") and not gq.values_match("2026-11-24", "")


def test_quality_rejects_substring_and_suffix_false_positives() -> None:
    assert not gq.keys_match(gq.norm("가나컴퍼니"), gq.norm("가나컴퍼니주"))
    assert not gq.keys_match(gq.norm("가나"), gq.norm("가나컴퍼니주식회사"))
    assert not gq.keys_match(gq.norm("김민수"), gq.norm("김민수정"))
    assert not gq.values_match("1234", "51234")  # 접미 일치는 맞다고 보지 않는다
    assert gq.values_match("2026-11-24", "11월 24일")  # 메일 본문 표기(연도 없음)는 맞다
    assert gq.values_match("2026-11-24", "2026년 11월 24일")
    assert not gq.values_match("2026-11-24", "2025-11-24")  # 연도가 양쪽에 있으면 같아야 한다
    assert not gq.values_match("2026-11-24", "11월 25일") and not gq.values_match(
        "2026-11-24", "11/2"
    )
    assert gq.values_match("2026-11-24", "2026-11-24")
    assert gq.values_match("5370000원", "537만 원") and gq.values_match("537만 원", "5,370,000원")
    assert not gq.values_match("5370000원", "15370000원") and not gq.values_match("5370000원", "")
    assert gq.QUALITY_VERSION == "rag-a4-quality-v3"


def test_evaluate_quality_counts_recall_precision_and_failure_rate() -> None:
    gold = [
        {
            "subject": "송다은",
            "subject_type": "person",
            "predicate": "works_at",
            "object": "비올기획",
            "object_type": "company",
            "sources": ["m1"],
        },
        {
            "subject": "도넬딘",
            "subject_type": "project",
            "predicate": "deadline:중간 보고",
            "object": "2026-11-24",
            "object_type": "date",
            "sources": ["m2"],
        },
    ]
    ents = [
        gq.FoundEntity("송다은님", "person", ("m1",)),
        gq.FoundEntity("비올기획", "company", ("m1",)),
        gq.FoundEntity("도넬딘", "project", ("m2",)),
        gq.FoundEntity("잡음", "place", ("m1",)),
    ]
    rels = [
        gq.FoundRelation("m1", "송다은", "works_at", "비올기획", False),
        gq.FoundRelation("m2", "도넬딘", "mentioned_with", "2026-11-24", True),
        gq.FoundRelation("m3", "아무", "x", "누구", False),
    ]
    out = gq.evaluate_quality(gold, ents, rels, mails_total=4, format_errors=1)
    assert out["entity"]["recall"] == 1.0  # 정답 개체: 송다은, 비올기획, 도넬딘(날짜는 개체 아님)
    assert out["entity"]["precision_all"] == 0.75 and out["entity"]["precision_gold_mails"] == 0.75
    assert out["relation"]["recall_loose"] == 1.0
    assert out["relation"]["recall_strict"] == 0.5  # 둘째는 관계 이름이 deadline 계열이 아니다
    assert out["relation"]["found_in_gold_mails"] == 2  # m3은 정답 출처 메일이 아니다
    assert out["relation"]["precision_gold_mails_strict"] == 0.5
    assert out["format_error_rate"] == 0.25


def test_evaluate_quality_empty_extraction_gives_zero_not_error() -> None:
    gold = [
        {
            "subject": "a가",
            "subject_type": "person",
            "predicate": "works_at",
            "object": "b나",
            "object_type": "company",
            "sources": ["m1"],
        }
    ]
    out = gq.evaluate_quality(gold, [], [], mails_total=1, format_errors=0)
    assert out["entity"]["recall"] == 0.0 and out["entity"]["precision_all"] is None


def test_token_cost_and_unknown_price() -> None:
    price = gb.PriceSettings(llm_price_input_per_mtok=0.1, llm_price_output_per_mtok=0.5)
    assert gb.token_cost(price, 1_000_000, 1_000_000) == 0.6
    assert gb.token_cost(None, 10, 10) is None
    assert gb.token_cost(gb.PriceSettings(), 10, 10) is None


class Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def test_update_cost_adds_extraction_and_affected_community_summaries() -> None:
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    raw = {
        "extractions": [
            Row(source_item_id=a, status="ok", input_tokens=100, output_tokens=10, cost_usd=0.01),
            Row(source_item_id=b, status="ok", input_tokens=100, output_tokens=10, cost_usd=None),
        ],
        "communities": [
            Row(source_item_ids=[a, b], input_tokens=50, output_tokens=5, cost_usd=0.02),
        ],
    }
    out = gb.update_cost(raw)
    assert out["mails"] == 2
    assert out["usd"]["max"] == 0.03 and out["usd"]["p50"] in (0.02, 0.03)
    assert out["calls"]["mean"] == 2.0
    assert out["unknown_cost_mails"] == 1  # 비용을 모르는 호출은 센다


def test_graph_digest_mixed_entity_and_value_objects() -> None:
    e1, e2 = uuid.UUID(int=1), uuid.UUID(int=2)
    ents = [
        store.EntityRow(e1, "가가", "가가", "person"),
        store.EntityRow(e2, "나나", "나나", "company"),
    ]
    by_entity = store.RelationRow(e1, e1, "가가", "p", e2, "나나", None, e1, "m1")
    by_value = store.RelationRow(e2, e1, "가가", "p", None, "값", "값", e1, "m1")
    rels = [by_entity, by_value]
    d = gb.graph_digest(ents, rels, [])
    assert d == gb.graph_digest(ents, rels[::-1], [])
    assert d == gb.graph_digest(ents[::-1], rels, [])
    # 개체 객체와 값 객체는 서로 다른 digest를 낸다.
    assert d != gb.graph_digest(ents, [by_entity], [])
    assert gb.graph_digest(ents, [by_value], []) != gb.graph_digest(ents, [by_entity], [])
    # 같은 문자열이라도 개체 객체(object_id)와 값 객체(object_value)는 digest가 다르다.
    as_entity = store.RelationRow(e1, e1, "가가", "p", e2, "나나", None, e1, "m1")
    as_value = store.RelationRow(e1, e1, "가가", "p", None, "나나", "나나", e1, "m1")
    assert gb.graph_digest(ents, [as_entity], []) != gb.graph_digest(ents, [as_value], [])
    # object_value가 빈 문자열인 관계와 None인 관계도 구분된다.
    empty = store.RelationRow(e1, e1, "가가", "p", None, "", "", e1, "m1")
    none = store.RelationRow(e1, e1, "가가", "p", None, None, None, e1, "m1")
    assert gb.graph_digest(ents, [empty], []) != gb.graph_digest(ents, [none], [])


def test_graph_digest_is_stable_and_sensitive() -> None:
    e1, e2 = uuid.UUID(int=1), uuid.UUID(int=2)
    ents = [
        store.EntityRow(e1, "가가", "가가", "person"),
        store.EntityRow(e2, "나나", "나나", "company"),
    ]
    rels = [store.RelationRow(e1, e1, "가가", "works_at", e2, "나나", None, e1, "m1")]
    base = gb.graph_digest(ents, rels, [("k", "요약")])
    assert base == gb.graph_digest(ents[::-1], rels, [("k", "요약")])
    assert base != gb.graph_digest(ents, rels, [("k", "다른 요약")])


def test_dry_run_estimate_counts_calls_without_prices() -> None:
    pytest.importorskip("networkx")
    mail = mk_mail("m", "b" * 100)
    out = gb.dry_run_estimate([mail, mail], [("가", "나"), ("다", "라")], None)
    assert (
        out["extraction"]["calls"] == 2 and out["summary"]["calls_from_gold_graph_lower_bound"] == 2
    )
    assert out["price_known"] is False and out["total_cost_usd"] is None


class _Index:
    def config_info(self) -> dict[str, Any]:
        return {"stub": True}


def test_a4_sut_requires_graph_and_valid_mode_and_fingerprints_conditions() -> None:
    assert "A4" in ARCHS
    with pytest.raises(ValueError, match="graph_digests"):
        RagArchSUT(rag_arch="A4", index=_Index(), reranker=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="a4-mode"):
        RagArchSUT(
            rag_arch="A4",
            index=_Index(),  # type: ignore[arg-type]
            graph_digests={"a": "d"},
            a4_mode="both",
        )

    def info(**kw: Any) -> dict[str, Any]:
        sut = RagArchSUT(rag_arch="A4", index=_Index(), graph_digests={"a": "d1"}, **kw)  # type: ignore[arg-type]
        return sut.config_info(model_id="m")

    base = info()
    assert base["extract_prompt_sha"] == prompt_sha(EXTRACT_PROMPT)
    assert base["summary_prompt_sha"] == prompt_sha(SUMMARY_PROMPT)
    assert base["route_prompt_sha"] == prompt_sha(ROUTE_PROMPT)
    assert base["graph_digests"] == {"a": "d1"} and base["louvain_seed"] and base["hops"] == 2
    assert info(a4_mode="local")["condition_digest"] != base["condition_digest"]


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-1", "-0.5", "abc"])
def test_cli_rejects_non_finite_or_non_positive_cost_cap(bad: str) -> None:
    from orbit.evalrunner.rag_arch.graph import cli as graph_cli  # noqa: PLC0415

    with pytest.raises(SystemExit) as info:
        graph_cli.main(["build", "--dataset", "x", "--max-cost-usd", bad])
    assert info.value.code == 2


def test_cli_cost_cap_default_is_one_dollar_and_positive_values_pass() -> None:
    from orbit.evalrunner.rag_arch.graph import cli as graph_cli  # noqa: PLC0415

    parser = graph_cli._parser()
    assert parser.parse_args(["build", "--dataset", "x"]).max_cost_usd == 1.0
    assert (
        parser.parse_args(["build", "--dataset", "x", "--max-cost-usd", "0.25"]).max_cost_usd
        == 0.25
    )


@pytest.mark.parametrize(
    "name",
    [
        "MAX_ENTITIES_PER_MAIL",
        "MAX_RELATIONS_PER_MAIL",
        "MAX_NAME_CHARS",
        "MAX_PREDICATE_CHARS",
        "EXTRACT_MAX_OUTPUT_TOKENS",
        "MAX_SUMMARY_RELATIONS",
        "SUMMARY_MAX_OUTPUT_TOKENS",
    ],
)
def test_limit_constants_change_the_tags_and_digest(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (gb.extract_tag("m"), gb.summary_tag("m", "e"), gb.graph_digest([], [], []))
    monkeypatch.setattr(gb, name, getattr(gb, name) + 1)
    after = (gb.extract_tag("m"), gb.summary_tag("m", "e"), gb.graph_digest([], [], []))
    assert before[2] != after[2]
    assert (before[0] != after[0]) or (before[1] != after[1])
