"""rag-v1 계획(`rag-plan.json`) 스키마. 구조 생성기가 쓰고 질문 생성기·검수·통계가 읽는다.

계획은 skeleton의 의미 정보다. 메일 ID는 skeleton의 `SkMessage.id`와 같다. 필드를 바꾸면
`RAG_SCHEMA`를 올린다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

RAG_SCHEMA = "orbit-rag-v1/3"
QTYPES: tuple[str, ...] = ("single", "multihop", "aggregate", "unanswerable")
QType = Literal["single", "multihop", "aggregate", "unanswerable"]


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GoldFact(_M):
    fact_id: str
    any_of: list[str]
    description: str


class Deadline(_M):
    item: str
    day: str  # ISO 날짜
    message_id: str
    forms: list[str]


class Cluster(_M):
    """프로젝트 하나와 그 메일 묶음. 질문 4개(유형별 1개)가 이 묶음을 공유한다."""

    cluster_id: str
    account_id: str
    index: int
    family: str
    split: Literal["dev", "test"]
    project: str
    vendor: str
    contact: str
    contact_email: str
    lead: str
    place: str
    decision: str
    quote_won: int
    assign_id: str
    kick_id: str
    decision_id: str
    quote_id: str
    thread_ids: list[str]  # 협력사 담당자와 직접 주고받은 메일 전부(견적 메일 포함)
    deadlines: list[Deadline]
    missing_item: str  # 이 프로젝트에 마감 메일이 없는 항목
    deposit_id: str | None
    deposit_won: int | None
    mail_ids: list[str]  # 프로젝트 메일 전부(배경 메일 제외, 잡담 포함)


class Blueprint(_M):
    """질문 하나의 명세. 질문 문장은 템플릿이고 gold는 구조에서 계산한 값이다."""

    task_id: str
    scenario_id: str
    cluster_id: str
    account_id: str
    qtype: QType
    variant: str
    family: str
    question: str
    keep: list[str]  # 재작성 때 그대로 남길 표현
    hidden: list[str]  # 질문에 나오면 안 되는 중간 값(gold 값은 facts에 있다)
    absent: list[
        str
    ] = []  # 답할 수 없음 문항: 이 중 하나라도 프로젝트 메일에 있으면 안 되는 표현(any-of)
    facts: list[GoldFact]
    evidence: list[str]
    decoys: list[str]
    hops: int


class Triple(_M):
    account_id: str
    subject: str
    subject_type: str
    predicate: str
    object: str
    object_type: str
    sources: list[str]


class Plan(_M):
    schema_version: str = RAG_SCHEMA
    seed: int
    generator_git_sha: str
    config_sha256: str
    skeleton_sha256: str
    user_emails: dict[str, str]
    clusters: list[Cluster]
    blueprints: list[Blueprint]
    triples: list[Triple]

    def cluster(self, cluster_id: str) -> Cluster:
        return next(c for c in self.clusters if c.cluster_id == cluster_id)
