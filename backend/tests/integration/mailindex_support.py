"""메일 색인 통합 테스트 공통 도구. 데이터는 모두 합성 값이다.

실제 PostgreSQL(`orbit_test`)에 합성 연결·색인을 만들고 Sim 메일(`SimMailConnector`)과 결정적 가짜
임베더(`FakeEmbedder`)로 동기화한다. 시계는 `FixedClock`이다.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, text

from integration.support import WorkspaceFactory
from orbit.connectors.contracts import Address, MailMessage
from orbit.connectors.sim.factory import SimConnectorSet, build_sim_connectors
from orbit.connectors.sim.mail import SimMailConnector
from orbit.connectors.sim.world import AccountState, SimWorld
from orbit.core.clock import FixedClock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.mailindex import service
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import FakeEmbedder
from orbit.mailindex.search import SearchHit, search_index
from orbit.mailindex.tokenize import tokenize

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
ACCOUNT = "acct-alice"
WORLD_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "sim" / "minimal_world.json"
SUB_PREFIX = "test-sub-mailindex-"
DUMMY_KEK = "test-kek"


class CountingEmbedder(FakeEmbedder):
    """임베딩한 텍스트 수를 센다(재임베딩 여부 확인용)."""

    def __init__(self) -> None:
        super().__init__()
        self.embedded = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return await super().embed(texts)


def mail(
    message_id: str,
    subject: str,
    body: str,
    *,
    labels: tuple[str, ...] = ("INBOX",),
    sent_at: datetime = NOW - timedelta(days=2),
    sender: str = "bob@example.org",
) -> MailMessage:
    return MailMessage(
        id=message_id,
        thread_id=f"thr-{message_id}",
        from_=Address(email=sender, name="합성 발신자"),
        to=(Address(email="alice@example.com"),),
        subject=subject,
        body_text=body,
        sent_at=sent_at,
        labels=labels,
    )


QUOTE = mail(
    "m-quote", "견적서 납기 협의", "요청하신 견적서를 보냅니다. 납기는 다음 주 금요일입니다."
)
PRICE = mail("m-price", "견적 가격 인하 요청", "견적 단가를 조금 낮춰 주실 수 있을까요?")
LUNCH = mail("m-lunch", "점심 메뉴 안내", "이번 주 점심은 김밥과 우동입니다.")
EXTRAS = (QUOTE, PRICE, LUNCH)


def create_connection(owner: Engine, ws: uuid.UUID, ids: Uuid7Generator) -> uuid.UUID:
    """합성 사용자·멤버십·활성 연결을 만든다(소유자 역할. orbit_app은 만들 수 없다)."""
    user, cid = ids.next(), ids.next()
    with owner.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws)})
        conn.execute(
            text(
                "INSERT INTO app_user (id, google_sub, email, display_name, created_at,"
                " last_login_at) VALUES (:id, :sub, 'x@example.test', 'x', :now, :now)"
            ),
            {"id": user, "sub": f"{SUB_PREFIX}{user}", "now": NOW},
        )
        conn.execute(
            text("INSERT INTO membership (workspace_id, user_id, role) VALUES (:w, :u, 'owner')"),
            {"w": ws, "u": user},
        )
        conn.execute(
            text(
                "INSERT INTO connection (id, workspace_id, user_id, provider, account_email,"
                " account_sub, scopes, status, token_ciphertext, token_nonce, wrapped_dek, kek_id,"
                " created_at, updated_at) VALUES (:id, :ws, :uid, 'google', 'alice@example.com',"
                " 'sub', ARRAY[]::text[], 'active', '\\x01', '\\x02', '\\x03', :kek, :now, :now)"
            ),
            {"id": cid, "ws": ws, "uid": user, "kek": DUMMY_KEK, "now": NOW},
        )
    return cid


def cleanup_users(owner: Engine) -> None:
    with owner.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE google_sub LIKE :p"), {"p": SUB_PREFIX + "%"})


@dataclass
class IndexEnv:
    db: AsyncDatabase
    ws: uuid.UUID
    cid: uuid.UUID
    clock: FixedClock
    ids: Uuid7Generator
    embedder: CountingEmbedder
    mi: MailIndexDeps
    world: SimWorld
    conns: SimConnectorSet
    mail: SimMailConnector
    state: AccountState
    extra_ws: list[uuid.UUID] = field(default_factory=list)

    async def scalar(self, sql: str, **params: object) -> int:
        async with self.db.workspace_session(self.ws) as s:
            return int((await s.execute(text(sql), params)).scalar_one())

    async def item_ids(self) -> set[str]:
        async with self.db.workspace_session(self.ws) as s:
            rows = await s.execute(text("SELECT provider_item_id FROM source_item"))
            return {r[0] for r in rows}

    async def search(
        self, query: str, *, limit: int = 5, vector: bool = True, keyword: bool = True
    ) -> list[SearchHit]:
        embedding = (await self.embedder.embed([query]))[0] if vector else None
        tokens = tokenize(query) if keyword else []
        async with self.db.workspace_session(self.ws) as s:
            return await search_index(s, self.ws, embedding=embedding, tokens=tokens, limit=limit)


async def build_env(
    db: AsyncDatabase,
    workspaces: WorkspaceFactory,
    owner: Engine,
    *,
    extra: Sequence[MailMessage] = (),
) -> IndexEnv:
    ws = workspaces.create()
    clock = FixedClock(NOW)
    ids = Uuid7Generator(clock)
    cid = create_connection(owner, ws, ids)
    world = SimWorld.load(WORLD_PATH, clock)
    state = world.account(ACCOUNT)
    for message in extra:
        state.messages[message.id] = message  # 시드로 넣는다(이력에 남기지 않는다)
        state.threads.setdefault(message.thread_id, []).append(message.id)
    conns = build_sim_connectors(world, [ACCOUNT])
    embedder = CountingEmbedder()
    mi = MailIndexDeps(db=db, clock=clock, ids=ids, embedder=embedder)
    return IndexEnv(db, ws, cid, clock, ids, embedder, mi, world, conns, conns.mail[ACCOUNT], state)


async def add_inbox(env: IndexEnv, days: int = 365) -> None:
    await service.add_scope(
        env.mi, env.ws, env.cid, label_id="INBOX", label_name="INBOX", period_days=days
    )


async def purge_jobs(db: AsyncDatabase, ws: uuid.UUID) -> None:
    async with db.workspace_session(ws) as s:
        await s.execute(
            text("DELETE FROM procrastinate_jobs WHERE args->>'workspace_id' = :ws"),
            {"ws": str(ws)},
        )


async def env_iter(
    db: AsyncDatabase, workspaces: WorkspaceFactory, owner: Engine, **kw: object
) -> AsyncIterator[IndexEnv]:
    env = await build_env(db, workspaces, owner, **kw)  # type: ignore[arg-type]
    try:
        yield env
    finally:
        await purge_jobs(db, env.ws)
