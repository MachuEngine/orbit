"""색인 작업이 쓰는 주입 의존성 (ADR-0019). 시계·ID·임베더·토큰화기는 모두 주입한다."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import procrastinate

from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.mailindex.embedding import Embedder
from orbit.mailindex.tokenize import tokenize


@dataclass
class MailIndexDeps:
    db: AsyncDatabase
    clock: Clock
    ids: Uuid7Generator
    embedder: Embedder
    tokenize: Callable[[str], list[str]] = tokenize
    sync_interval: timedelta = field(default_factory=lambda: timedelta(minutes=15))
    error_backoff: timedelta = field(default_factory=lambda: timedelta(hours=1))
    query_stale: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    query_sync_timeout_s: float = 10.0
    jobs: procrastinate.App | None = None  # 있으면 동기화 작업을 등록한다
