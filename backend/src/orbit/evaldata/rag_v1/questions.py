"""rag-v1 질문 세트: 묶음마다 4유형 질문 1개씩. 정답·근거는 구조에서 코드가 계산한다.

- single: 메일 한 통에 있는 사실 하나(킥오프 장소 또는 결정 사항). 근거 1통.
- multihop: 담당 안내 메일에서 협력사 담당자를 찾고, 그 사람이 보낸 견적 메일의 금액을 답한다.
  근거 2통. 질문은 중간 값(담당자 이름·협력사)을 말하지 않고 견적 메일은 프로젝트 이름이 없다.
- aggregate: 프로젝트의 마감일 전부(항목별 사실) 또는 협력사 담당자와 주고받은 메일 전부와 건수.
- unanswerable: 메일에 없는 사실(없는 마감 항목, 보증금 메일이 없는 프로젝트의 보증금). 정답은 기권.

질문 문장은 코드 템플릿이다. 표현 변형은 `orbit.evaldata.v1.paraphrase`가 한 번 쓰고, 검증을
통과하지 못하면 템플릿 문장을 쓴다. 정답 값이나 중간 값이 질문에 들어가면 실패다(`leaked_terms`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from orbit.evaldata.rag_v1.model import Blueprint, Cluster, GoldFact, Plan
from orbit.evaldata.schema import (
    AbstainAnswer,
    AnswerFact,
    Gold,
    MessageRef,
    MessageSetAnswer,
    Task,
    Turn,
)
from orbit.evaldata.v1.facts import amount_fact, norm
from orbit.evaldata.v1.paraphrase import Source

KST = timezone(timedelta(hours=9))
AS_OF = datetime(2026, 11, 10, 10, 0, tzinfo=KST)  # 스냅샷(2026-11-10T01:00Z)과 같은 순간
TIME_ZONE = "Asia/Seoul"


class QuestionError(RuntimeError):
    """질문·정답이 규칙을 어긴다(누출, 근거 없음 등)."""


# 흔한 낱말("보증금", "검수 완료")만 막으면 realize 재시도가 늘어 값을 말하는 형태로 좁힌다.
DEPOSIT_ABSENT = ("계약 보증금", "보증금 금액")


def deadline_absent(item: str) -> list[str]:
    return [f"{item} 마감", f"{item} 기한", f"{item} 예정일"]


def _gold(fact_id: str, forms: list[str], description: str) -> GoldFact:
    return GoldFact(fact_id=fact_id, any_of=forms, description=description)


def _bp(cl: Cluster, qtype: str, variant: str, **kw: Any) -> Blueprint:
    tag = cl.cluster_id.removeprefix("c-rag-")
    return Blueprint(
        task_id=f"rag-{qtype}-{tag}",
        scenario_id=f"sc-rag-{tag}-{qtype}",
        cluster_id=cl.cluster_id,
        account_id=cl.account_id,
        qtype=qtype,
        variant=variant,
        family=cl.family,
        **kw,
    )


def blueprints_for(cl: Cluster) -> list[Blueprint]:
    """묶음 하나에서 유형별 질문 하나씩. 변형은 묶음 번호로 번갈아 고른다."""
    j = cl.index
    out: list[Blueprint] = []

    if j % 2 == 0:  # single
        out.append(
            _bp(
                cl,
                "single",
                "place",
                question=f"{cl.project} 프로젝트 킥오프 회의는 어디서 하는지 알려줘",
                keep=[cl.project],
                hidden=[],
                facts=[_gold("place", [cl.place], "킥오프 회의 장소")],
                evidence=[cl.kick_id],
                decoys=[cl.decision_id],
                hops=1,
            )
        )
    else:
        out.append(
            _bp(
                cl,
                "single",
                "decision",
                question=f"{cl.project} 프로젝트 회의에서 정해진 결정 사항이 뭐야?",
                keep=[cl.project],
                hidden=[],
                facts=[_gold("decision", [cl.decision], "결정 사항")],
                evidence=[cl.decision_id],
                decoys=[cl.kick_id],
                hops=1,
            )
        )

    out.append(  # multihop: 안내 메일(담당자 이름) -> 그 담당자의 견적 메일(금액)
        _bp(
            cl,
            "multihop",
            "vendor_quote",
            question=f"{cl.project} 프로젝트 협력사 담당자가 보낸 견적 금액이 얼마야?",
            keep=[cl.project],
            hidden=[cl.contact, cl.vendor],
            facts=[_gold("quote", amount_fact("q", cl.quote_won).surface_forms, "견적 금액")],
            evidence=[cl.assign_id, cl.quote_id],
            decoys=[m for m in cl.thread_ids if m != cl.quote_id],
            hops=2,
        )
    )

    if j % 2 == 0:  # aggregate
        out.append(
            _bp(
                cl,
                "aggregate",
                "deadlines",
                question=f"{cl.project} 프로젝트에 안내된 마감일을 항목별로 전부 알려줘",
                keep=[cl.project],
                hidden=[],
                facts=[
                    _gold(f"dl{i}", d.forms, f"{d.item} 마감일") for i, d in enumerate(cl.deadlines)
                ],
                evidence=[d.message_id for d in cl.deadlines],
                decoys=[cl.kick_id],
                hops=1,
            )
        )
    else:
        n = len(cl.thread_ids)
        out.append(
            _bp(
                cl,
                "aggregate",
                "thread_count",
                question=f"{cl.contact}님과 직접 주고받은 메일을 전부 찾아서 몇 통인지 알려줘",
                keep=[cl.contact],
                hidden=[],
                facts=[_gold("count", [f"{n}통", f"{n}건"], "주고받은 메일 수")],
                evidence=list(cl.thread_ids),
                decoys=[cl.assign_id],
                hops=1,
            )
        )

    if cl.deposit_id is None and j % 2 == 1:  # unanswerable
        out.append(
            _bp(
                cl,
                "unanswerable",
                "no_deposit",
                question=f"{cl.project} 프로젝트 계약 보증금은 얼마야?",
                keep=[cl.project],
                hidden=[],
                absent=list(DEPOSIT_ABSENT),
                facts=[],
                evidence=[],
                decoys=[cl.assign_id, cl.quote_id],
                hops=1,
            )
        )
    else:
        out.append(
            _bp(
                cl,
                "unanswerable",
                "missing_deadline",
                question=f"{cl.project} 프로젝트 {cl.missing_item} 마감일은 언제야?",
                keep=[cl.project, cl.missing_item],
                hidden=[],
                absent=deadline_absent(cl.missing_item),
                facts=[],
                evidence=[],
                decoys=[d.message_id for d in cl.deadlines],
                hops=1,
            )
        )
    return out


# ---------------------------------------------------------------- 검사


def leaked_terms(bp: Blueprint, question: str) -> list[str]:
    """질문에 들어간 정답 값·중간 값. 비어 있어야 한다."""
    text = norm(question)
    banned = [f for g in bp.facts for f in g.any_of] + bp.hidden
    return [t for t in dict.fromkeys(banned) if norm(t) in text]


def missing_keeps(bp: Blueprint, question: str) -> list[str]:
    text = norm(question)
    return [k for k in bp.keep if norm(k) not in text]


def world_problems(plan: Plan, bodies: Mapping[str, Mapping[str, str]]) -> list[str]:
    """실현된 본문을 정답과 대조한다. 원문은 싣지 않고 문항·메일 ID만 적는다.

    `bodies`는 계정 ID -> (메일 ID -> 본문)이다. 정답 사실은 근거 메일 중 어느 하나에 있어야 하고,
    답할 수 없음 문항의 표현은 그 프로젝트 메일(이름이 언급된 메일 포함)에 없어야 한다.
    """
    problems: list[str] = []
    for bp in plan.blueprints:
        mails = bodies.get(bp.account_id, {})
        cl = plan.cluster(bp.cluster_id)
        missing = [m for m in bp.evidence if m not in mails]
        if missing:
            problems.append(f"{bp.task_id}: 근거 메일 {len(missing)}통이 세계에 없다")
            continue
        if bp.variant != "thread_count":  # 건수는 헤더로 정한다
            for g in bp.facts:
                if not any(norm(f) in norm(mails[m]) for m in bp.evidence for f in g.any_of):
                    problems.append(f"{bp.task_id}: 사실 {g.fact_id}가 근거 본문에 없다")
        if bp.qtype == "multihop":
            if norm(cl.project) in norm(mails[cl.quote_id]):
                problems.append(f"{bp.task_id}: 견적 메일 본문에 프로젝트 이름이 있다")
            if norm(cl.contact) not in norm(mails[cl.assign_id]):
                problems.append(f"{bp.task_id}: 담당 안내 메일에 담당자 이름이 없다")
        if bp.absent:
            problems += _absent_problems(plan, bp, cl, mails)
    return problems


def _absent_problems(plan: Plan, bp: Blueprint, cl: Cluster, mails: Mapping[str, str]) -> list[str]:
    """답할 수 없음 문항의 표현(any-of)이 본문에 있는지 본다. 배경 메일은 대상이 아니다.

    실패: (1) 이 프로젝트 메일에 있다, (2) 프로젝트 이름이 같이 나온 메일에 있다,
    (3) 어느 프로젝트의 메일이든 프로젝트 이름 없이 있다(어느 프로젝트 이야기인지 알 수 없다).
    """
    names = [norm(c.project) for c in plan.clusters if c.account_id == bp.account_id]
    mine = set(cl.mail_ids)
    project_mails = {m for c in plan.clusters if c.account_id == bp.account_id for m in c.mail_ids}
    out: list[str] = []
    for mid, text in sorted(mails.items()):
        body = norm(text)
        if not any(norm(t) in body for t in bp.absent):
            continue
        with_mine = norm(cl.project) in body
        if (
            (mid in mine)
            or with_mine
            or (mid in project_mails and not any(n in body for n in names))
        ):
            out.append(f"{bp.task_id}: 없어야 할 표현이 메일 {mid}에 있다")
    return out


def shared_evidence_problems(tasks: Sequence[Task]) -> list[str]:
    """다른 묶음의 질문과 근거 메일이 겹치면 문제다."""
    owner: dict[str, str] = {}
    out: list[str] = []
    for t in tasks:
        for tag in t.tags:
            if (
                tag.startswith("ref_message:")
                and owner.setdefault(tag, t.cluster_id) != t.cluster_id
            ):
                out.append(f"{t.task_id}: 근거 {tag}가 다른 묶음과 겹친다")
    return out


# ---------------------------------------------------------------- Task


def pick_question(bp: Blueprint, paraphrases: Mapping[str, str]) -> tuple[str, str]:
    """(질문, 출처). 변형문이 누출 없이 보존 표현을 지키면 그것을, 아니면 템플릿을 쓴다."""
    text = paraphrases.get(bp.task_id)
    if text and not leaked_terms(bp, text) and not missing_keeps(bp, text):
        return text, "paraphrase"
    return bp.question, "template"


def build_tasks(plan: Plan, paraphrases: Mapping[str, str] | None = None) -> list[Task]:
    tasks: list[Task] = []
    for bp in plan.blueprints:
        leaks = leaked_terms(bp, bp.question)
        if leaks:
            raise QuestionError(f"{bp.task_id}: 템플릿 질문에 정답·중간 값 {len(leaks)}개가 있다")
        question, source = pick_question(bp, paraphrases or {})
        cl = plan.cluster(bp.cluster_id)
        expected: AbstainAnswer | MessageSetAnswer
        if bp.qtype == "unanswerable":
            expected = AbstainAnswer()
        else:
            expected = MessageSetAnswer(messages=[MessageRef(message_id=m) for m in bp.evidence])
        tasks.append(
            Task(
                task_id=bp.task_id,
                scenario_id=bp.scenario_id,
                cluster_id=bp.cluster_id,
                split=cl.split,
                as_of_time=AS_OF,
                account_id=bp.account_id,
                turns=[Turn(role="user", content=question)],
                gold=Gold(
                    answer_facts=[
                        AnswerFact(fact_id=g.fact_id, any_of=g.any_of, description=g.description)
                        for g in bp.facts
                    ],
                    expected_answer=expected,
                    answer_facts_required=bp.qtype != "unanswerable",
                ),
                difficulty={
                    "hops": bp.hops,
                    "evidence_mails": len(bp.evidence),
                    "question_source": source,
                },
                tags=[
                    f"rag:{bp.qtype}",
                    f"variant:{bp.variant}",
                    f"family:{bp.family}",
                    f"user:{bp.account_id}",
                    "derived:none",
                    *(f"ref_message:{m}" for m in sorted(bp.evidence)),
                ],
                time_zone=TIME_ZONE,
            )
        )
    return tasks


def sources_for(plan: Plan) -> list[Source]:
    return [
        Source(origin_id=bp.task_id, question=bp.question, keep=tuple(bp.keep), family=bp.family)
        for bp in plan.blueprints
    ]
