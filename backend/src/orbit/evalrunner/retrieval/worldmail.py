"""평가용 색인이 읽는 Sim 세계의 메일: 색인 범위와 시각을 한곳에서 정한다 (ADR-0028 §1)."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orbit.connectors.contracts import MailMessage
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.mailindex.chunking import index_text, split_chunks

# 색인 시각. 세계의 마지막 메일(2026-11-09)보다 뒤이고 검색 세트의 as_of보다 앞서지 않아도 된다
# (색인 범위 판정에만 쓴다). 바꾸면 색인 지문이 바뀐다.
INDEX_NOW = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
SCOPE_LABELS = ("INBOX", "SENT")  # 세계의 메일은 모두 둘 중 하나(또는 INBOX와 카테고리)다
SCOPE_PERIOD_DAYS = 365
# 평가 실행기의 첫 동기화 상한. 5,000통 메일함의 INBOX(3천여 통)를 다 담는다. 운영 값은 2,000이다.
EVAL_FIRST_SYNC_MAX = 10_000


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_world(path: Path) -> SimWorld:
    return SimWorld.load(path, FixedClock(INDEX_NOW))


def in_scope(message: MailMessage) -> bool:
    """색인 대상: 범위 라벨이 하나라도 있고 기간 안이며 색인할 텍스트가 있다(동기화와 같은 규칙)."""
    if not any(label in message.labels for label in SCOPE_LABELS):
        return False
    if message.sent_at < INDEX_NOW - timedelta(days=SCOPE_PERIOD_DAYS):
        return False
    return bool(index_text(message.subject, message.body_text))


def mailbox_messages(world: SimWorld, account_id: str) -> list[MailMessage]:
    """계정에서 색인되는 메일(ID순)."""
    messages = world.account(account_id).messages.values()
    return sorted((m for m in messages if in_scope(m)), key=lambda m: m.id)


def mailbox_chunks(world: SimWorld, account_id: str) -> list[str]:
    """색인되는 모든 청크 텍스트(메일 순서, 메일 안 순서)."""
    return [
        c for m in mailbox_messages(world, account_id) for c in split_chunks(m.subject, m.body_text)
    ]
