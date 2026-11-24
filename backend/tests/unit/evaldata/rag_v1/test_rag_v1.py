"""rag-v1 생성기 테스트: 결정성, 유형 수, 근거 계산, 누출 검사, 분할, 긴 근거 비율, 채점."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from orbit.connectors.sim.world import WorldDiff
from orbit.evaldata import dataset as ds
from orbit.evaldata.rag_v1.build import MAILS_PER_ACCOUNT, N_ACCOUNTS, _deadline_days, build
from orbit.evaldata.rag_v1.cli import EXIT_OK, EXIT_PLAN, EXIT_USAGE, main
from orbit.evaldata.rag_v1.model import Blueprint, Cluster, Plan
from orbit.evaldata.rag_v1.questions import (
    build_tasks,
    leaked_terms,
    shared_evidence_problems,
    world_problems,
)
from orbit.evaldata.rag_v1.stats import compute_stats
from orbit.evaldata.schema import Counts, Manifest, SplitRatio, Task
from orbit.evaldata.v1.build import skeleton_json
from orbit.evaldata.v1.export import export_world
from orbit.evaldata.v1.facts import conflicts, date_fact, norm
from orbit.evaldata.v1.skeleton import Skeleton
from orbit.evalrunner.scoring import score_episode
from orbit.evalrunner.structured import grade_structured

SEED = 7
SHA = "test-sha"


@pytest.fixture(scope="module")
def built() -> tuple[Skeleton, Plan]:
    return build(SEED, SHA)


@pytest.fixture(scope="module")
def tasks(built: tuple[Skeleton, Plan]) -> list[Task]:
    return build_tasks(built[1])


def _msgs(sk: Skeleton) -> dict[str, tuple[str, str, tuple[str, ...], str]]:
    return {
        m.id: (a, m.from_.email, tuple(x.email for x in m.to), m.subject)
        for a, acct in sk.accounts.items()
        for m in acct.messages
    }


def test_same_seed_same_bytes_and_other_seed_differs(built: tuple[Skeleton, Plan]) -> None:
    sk2, plan2 = build(SEED, SHA)
    assert skeleton_json(sk2) == skeleton_json(built[0])
    assert plan2.model_dump_json() == built[1].model_dump_json()
    assert skeleton_json(build(SEED + 1, SHA)[0]) != skeleton_json(built[0])


def test_accounts_and_mail_counts(built: tuple[Skeleton, Plan]) -> None:
    sk, _ = built
    assert len(sk.accounts) == N_ACCOUNTS
    assert {len(a.messages) for a in sk.accounts.values()} == {MAILS_PER_ACCOUNT}
    ids = [m.id for a in sk.accounts.values() for m in a.messages]
    assert len(ids) == len(set(ids))
    assert all(m.body_id in sk.bodies for a in sk.accounts.values() for m in a.messages)


def test_type_counts_and_split(tasks: list[Task]) -> None:
    assert len(tasks) == 160
    kinds = Counter(next(t for t in x.tags if t.startswith("rag:")) for x in tasks)
    assert kinds == {f"rag:{k}": 40 for k in ("single", "multihop", "aggregate", "unanswerable")}
    split = Counter((next(t for t in x.tags if t.startswith("rag:")), x.split) for x in tasks)
    assert {v for (_, s), v in split.items() if s == "dev"} == {16}
    assert {v for (_, s), v in split.items() if s == "test"} == {24}


def test_multihop_and_aggregate_evidence_recomputed_from_headers(
    built: tuple[Skeleton, Plan],
) -> None:
    sk, plan = built
    msgs = _msgs(sk)
    for bp in plan.blueprints:
        cl = plan.cluster(bp.cluster_id)
        if bp.qtype == "multihop":
            assert bp.hops == 2
            assert bp.evidence == [cl.assign_id, cl.quote_id]
            # 견적 메일은 협력사 담당자가 사용자에게 보냈고 본문에 프로젝트 이름을 쓰지 않는다
            assert msgs[cl.quote_id][1] == cl.contact_email
            quote_body_id = next(
                m.body_id for m in sk.accounts[cl.account_id].messages if m.id == cl.quote_id
            )
            assert cl.project in {f.value for f in sk.bodies[quote_body_id].forbidden_facts}
            assert cl.contact not in bp.question and cl.vendor not in bp.question
        if bp.variant == "thread_count":
            by_header = {
                i
                for i, (acct, frm, to, _) in msgs.items()
                if acct == cl.account_id and cl.contact_email in (frm, *to)
            }
            assert set(bp.evidence) == by_header
            assert any(f"{len(by_header)}통" in g.any_of for g in bp.facts)
        if bp.variant == "deadlines":
            by_subject = {
                i
                for i, (acct, _, _, subj) in msgs.items()
                if acct == cl.account_id and subj.startswith(f"[{cl.project}]") and "마감" in subj
            }
            assert set(bp.evidence) == by_subject
            assert len(bp.facts) == len(by_subject) == 3
            assert cl.missing_item not in {d.item for d in cl.deadlines}
            assert not any(cl.missing_item in g.description for g in bp.facts)


def test_aggregate_facts_required_and_unanswerable_is_abstain(tasks: list[Task]) -> None:
    for t in tasks:
        kind = next(x for x in t.tags if x.startswith("rag:"))
        assert t.gold.answer_facts_required == (kind != "rag:unanswerable")
        if kind == "rag:unanswerable":
            assert t.gold.expected_answer is not None
            assert t.gold.expected_answer.kind == "abstain"
            assert not [x for x in t.tags if x.startswith("ref_message:")]


def test_leak_check(built: tuple[Skeleton, Plan]) -> None:
    plan = built[1]
    assert all(not leaked_terms(bp, bp.question) for bp in plan.blueprints)
    mh = next(b for b in plan.blueprints if b.qtype == "multihop")
    assert leaked_terms(mh, f"{mh.hidden[0]}님이 보낸 견적") == [mh.hidden[0]]
    single = next(b for b in plan.blueprints if b.qtype == "single")
    assert leaked_terms(single, f"{single.facts[0].any_of[0]}에서 하는 회의")
    # 누출된 변형문은 템플릿으로 되돌아간다
    tasks = build_tasks(plan, {mh.task_id: f"{mh.hidden[0]}님이 {mh.keep[0]} 견적 보냈지"})
    got = next(t for t in tasks if t.task_id == mh.task_id)
    assert got.turns[0].content == mh.question
    assert got.difficulty["question_source"] == "template"
    ok = f"{mh.keep[0]} 견적이 얼마였는지 말해줘"
    got = next(t for t in build_tasks(plan, {mh.task_id: ok}) if t.task_id == mh.task_id)
    assert got.turns[0].content == ok


def test_cluster_split_integrity(built: tuple[Skeleton, Plan], tasks: list[Task]) -> None:
    plan = built[1]
    by_cluster: dict[str, set[str]] = {}
    for t in tasks:
        by_cluster.setdefault(t.cluster_id, set()).add(t.split)
    assert len(by_cluster) == 40
    assert all(len(v) == 1 for v in by_cluster.values())
    assert shared_evidence_problems(tasks) == []
    # 계정마다 dev 묶음 4개
    dev = Counter(c.account_id for c in plan.clusters if c.split == "dev")
    assert set(dev.values()) == {4}


def test_long_evidence_ratio_at_least_30_percent(
    built: tuple[Skeleton, Plan], tasks: list[Task]
) -> None:
    sk, plan = built
    stats = compute_stats(plan, tasks, sk)
    assert stats["planned_long_evidence_ratio"] >= 0.30
    assert stats["realized"] is False


def test_dataset_loads_and_template_world_matches_gold(
    built: tuple[Skeleton, Plan], tasks: list[Task], tmp_path: Path
) -> None:
    sk, plan = built
    world = export_world(sk)
    nested = {
        k: {m["id"]: m["body_text"] for m in a["messages"]} for k, a in world["accounts"].items()
    }
    bodies = {m: t for a in nested.values() for m, t in a.items()}
    assert world_problems(plan, nested) == []
    first = plan.blueprints[0]
    acct = {**nested[first.account_id], first.evidence[0]: "무관한 본문"}
    assert world_problems(plan, {**nested, first.account_id: acct})
    manifest = Manifest(
        name="t",
        version="t",
        generator_git_sha=SHA,
        seed=SEED,
        created_with="templates",
        split_ratio=SplitRatio(dev=0.4, test=0.6),
        counts=Counts(
            users=len(world["accounts"]),
            messages=sum(len(a["messages"]) for a in world["accounts"].values()),
            events=0,
            tasks=len(tasks),
        ),
    )
    ds.write_dataset(tmp_path, manifest, world, tasks)
    loaded = ds.load_dataset(tmp_path)
    assert len(loaded.tasks) == 160
    stats = compute_stats(plan, tasks, sk, bodies)
    assert stats["realized"] is True
    assert stats["dev_evidence_length"]["max"] >= stats["dev_evidence_length"]["p50"]


def test_grade_structured_ok_and_abstain(tasks: list[Task]) -> None:
    for t in tasks:
        exp = t.gold.expected_answer
        assert exp is not None
        if exp.kind == "abstain":
            assert grade_structured(exp, {"kind": "abstain"}).verdict == "ok"
            assert grade_structured(exp, {"kind": "message_set", "messages": []}).verdict != "ok"
        else:
            ids = [m.message_id for m in exp.messages]  # type: ignore[union-attr]
            full = {"kind": "message_set", "messages": [{"message_id": i} for i in ids]}
            assert grade_structured(exp, full).verdict == "ok"
            assert grade_structured(exp, {"kind": "abstain"}).verdict != "ok"
            part = {"kind": "message_set", "messages": [{"message_id": ids[0]}]}
            if len(ids) > 1:
                assert grade_structured(exp, part).verdict != "ok"


def test_cli_offline_is_reproducible(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        assert (
            main(
                ["skeleton", "--out", str(d), "--seed", str(SEED), "--git-sha", SHA],
                echo=lambda _: None,
            )
            == EXIT_OK
        )
        assert (
            main(
                ["tasks", "--out", str(d), "--skip-paraphrase", "--unverified-bodies"],
                echo=lambda _: None,
            )
            == EXIT_OK
        )
        assert main(["review", "--out", str(d)], echo=lambda _: None) == EXIT_OK
        assert main(["stats", "--out", str(d)], echo=lambda _: None) == EXIT_OK
    for name in (
        "skeleton.json",
        "rag-plan.json",
        "graph_gold.json",
        "tasks.jsonl",
        "manifest.json",
        "review-packet.md",
    ):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    assert (
        main(["tasks", "--out", str(a), "--unverified-bodies"], echo=lambda _: None) == EXIT_USAGE
    )


def _score(t: Task, ids: list[str], text: str) -> bool:
    assert t.gold.expected_answer is not None
    got = {"kind": "message_set", "messages": [{"message_id": i} for i in ids]}
    return bool(score_episode(t.gold, text, [], WorldDiff(), got).success)


def test_multihop_needs_both_mails_and_right_values(
    built: tuple[Skeleton, Plan], tasks: list[Task]
) -> None:
    plan = built[1]
    for bp in plan.blueprints:
        if bp.qtype == "unanswerable":
            continue
        t = next(x for x in tasks if x.task_id == bp.task_id)
        right = " ".join(g.any_of[0] for g in bp.facts)
        assert _score(t, bp.evidence, right)
        assert not _score(t, bp.evidence, "모르겠다")  # 맞는 메일을 인용해도 값이 틀리면 실패
        if bp.qtype == "multihop":
            cl = plan.cluster(bp.cluster_id)
            assert bp.evidence == [cl.assign_id, cl.quote_id]
            assert not _score(t, [cl.quote_id], right)  # 견적 메일 한 통만 인용하면 실패
            assert not _score(t, [cl.assign_id], right)


def test_absent_facts_forbidden_in_specs_and_checked_in_world(built: tuple[Skeleton, Plan]) -> None:
    sk, plan = built
    world = export_world(sk)
    nested = {
        k: {m["id"]: m["body_text"] for m in a["messages"]} for k, a in world["accounts"].items()
    }
    seen = 0
    for bp in plan.blueprints:
        if bp.qtype != "unanswerable":
            assert not bp.absent
            continue
        seen += 1
        cl = plan.cluster(bp.cluster_id)
        assert bp.absent
        specs = {m.id: sk.bodies[m.body_id] for m in sk.accounts[cl.account_id].messages}
        for mid in cl.mail_ids:  # 프로젝트 메일 전부가 realize 검사에서 이 표현을 막는다
            forbidden = {f for x in specs[mid].forbidden_facts for f in x.surface_forms}
            assert set(bp.absent) <= forbidden, (bp.task_id, mid)

        def check(mid: str, text: str, bp: Blueprint = bp, cl: Cluster = cl) -> list[str]:
            acct = {**nested[cl.account_id], mid: text}
            return [
                p
                for p in world_problems(plan, {**nested, cl.account_id: acct})
                if mid in p and bp.task_id in p
            ]

        others = [
            c
            for c in plan.clusters
            if c.account_id == cl.account_id and c.cluster_id != cl.cluster_id
        ]
        project_mail = others[0].mail_ids[-1]
        bg = next(
            m
            for m in nested[cl.account_id]
            if m not in {x for c in plan.clusters for x in c.mail_ids}
        )
        term = bp.absent[0]
        assert check(cl.kick_id, f"{cl.project} {term}")  # 이 프로젝트 이름과 함께
        assert check(cl.kick_id, term)  # 이 프로젝트 메일
        assert check(project_mail, term)  # 다른 프로젝트 메일이지만 프로젝트 이름이 없다
        assert not check(project_mail, f"{others[0].project} {term}")  # 다른 프로젝트 이야기
        assert not check(bg, term)  # 배경 메일은 대상이 아니다
    assert seen == 40


def test_wrong_values_fail_even_with_right_mails(
    built: tuple[Skeleton, Plan], tasks: list[Task]
) -> None:
    plan = built[1]
    for bp in plan.blueprints:
        if bp.qtype == "unanswerable":
            continue
        other = next(
            o for o in plan.blueprints if o.variant == bp.variant and o.cluster_id != bp.cluster_id
        )
        wrong = " ".join(g.any_of[0] for g in other.facts)
        if all(any(f in wrong for f in g.any_of) for g in bp.facts):
            continue  # 우연히 같은 값이면 건너뛴다
        t = next(x for x in tasks if x.task_id == bp.task_id)
        assert not _score(t, bp.evidence, wrong), bp.task_id


def test_no_substring_collisions_between_gold_forms_in_cluster(
    built: tuple[Skeleton, Plan],
) -> None:
    plan = built[1]
    for cl in plan.clusters:
        forms = [
            (bp.task_id, g.fact_id, norm(f))
            for bp in plan.blueprints
            if bp.cluster_id == cl.cluster_id
            for g in bp.facts
            for f in g.any_of
        ]
        for i, (t1, f1, a) in enumerate(forms):
            for t2, f2, b in forms[i + 1 :]:
                if (t1, f1) != (t2, f2):
                    assert a not in b and b not in a, (cl.cluster_id, a, b)


def test_deadline_days_avoid_collisions() -> None:
    import random

    for n in range(200):
        days = _deadline_days(random.Random(n))  # noqa: S311
        facts = [date_fact(str(i), d) for i, d in enumerate(days)]
        assert not any(conflicts(a, b) for i, a in enumerate(facts) for b in facts[i + 1 :])
    assert conflicts(date_fact("a", date(2026, 12, 1)), date_fact("b", date(2026, 12, 14)))


def test_cli_requires_world_and_checks_long_ratio(tmp_path: Path) -> None:
    quiet = {"echo": lambda _: None}
    main(["skeleton", "--out", str(tmp_path), "--seed", str(SEED), "--git-sha", SHA], **quiet)
    args = ["tasks", "--out", str(tmp_path), "--skip-paraphrase"]
    assert main(args, **quiet) == EXIT_PLAN  # world.json 없음
    assert main([*args, "--unverified-bodies"], **quiet) == EXIT_OK
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["details"]["bodies_verified"] is False
    assert manifest["details"]["bodies_note"] == "본문 미검증"
    sk = Skeleton.model_validate_json((tmp_path / "skeleton.json").read_text(encoding="utf-8"))
    world = export_world(sk)  # 템플릿 본문은 짧아 긴 근거 비율이 기준 미만이다
    (tmp_path / "world.json").write_text(json.dumps(world, ensure_ascii=False), encoding="utf-8")
    assert main(args, **quiet) == EXIT_PLAN
    for a in world["accounts"].values():
        for m in a["messages"]:
            m["body_text"] += "\n" + "배경 설명 " * 200
    (tmp_path / "world.json").write_text(json.dumps(world, ensure_ascii=False), encoding="utf-8")
    assert main(args, **quiet) == EXIT_OK
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["details"]["bodies_verified"] is True
    assert manifest["details"]["long_evidence_ratio"] >= 0.3
    assert "미검증" in manifest["details"]["fact_late_position"]
