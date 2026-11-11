"""생성 중간 표현. 사건 타임라인의 일정·메일 기록과 아직 질문이 되지 않은 과제 명세."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

Contact = tuple[str, str]  # (이름, 이메일)

NOISE_KINDS = frozenset({"newsletter", "spam", "notice"})


@dataclass
class EventRec:
    id: str
    user: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str]  # 인물 key (사용자 본인은 항상 암묵적으로 참석)
    organizer: str  # 인물 key 또는 사용자 key
    location: str
    description: str
    version: str
    updated_at: datetime
    scenario_id: str
    status: str = "confirmed"
    project: str | None = None
    changes: int = 0  # 메일로 시간이 바뀐 횟수
    msg_ids: list[str] = field(default_factory=list)


@dataclass
class MailRec:
    id: str
    thread: str
    user: str
    sender: Contact
    to: list[Contact]
    subject: str
    body: str
    sent_at: datetime
    labels: list[str]
    kind: (
        str  # invite, change, confirm, cancel, deadline, followup, series, newsletter, spam, notice
    )
    scenario_id: str
    event_id: str | None = None


@dataclass
class TaskSpec:
    """질문·정답이 되기 전의 과제 명세. 정답은 최종 캘린더에서 계산한다."""

    kind: str
    scenario_id: str
    user: str
    as_of: datetime
    params: dict[str, Any]
    split: str = ""  # 계획된 분할(dev/test). 시나리오를 만들 때 정해진다
