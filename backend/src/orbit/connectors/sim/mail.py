"""Sim 메일 커넥터 (ADR-0008 4절). 모든 메서드는 `SimRuntime.run_tool`을 거친다.

SPIKE-005에서 관측한 Gmail 초안 동작을 재현한다.
- 초안 메시지는 `messages`에 들어 있지 않고 `drafts`에만 있다. 검색은 `in:drafts`일 때만 초안을
  (라벨 `DRAFT`인 메시지로 합성해) 돌려준다. 합성 메시지의 `sent_at`은 조회 시점의 clock이다.
- `send_draft`는 초안을 지우고 **새 메시지 ID**로 `SENT` 메시지를 만든다. 스레드 ID는 초안과 같고
  헤더(`X-Orbit-Execution-Id`)는 보존된다. 받는 사람에 계정 본인이 있으면 `INBOX`도 붙인다.
- 다른 계정으로의 배달은 흉내 내지 않는다(v0). 보낸 메일은 보내는 계정의 메일함에만 생긴다.
- `get_thread`는 초안 메시지를 포함하지 않는다. Gmail은 스레드 조회에 초안을 포함할 수 있으므로
  호출자는 `DRAFT` 라벨로 걸러야 한다.
- 초안 생성·발송·삭제는 `AccountState.draft_log`에 기록해 `diff`가 정확히 짝짓게 한다.
- 페이지 토큰은 정렬된 결과의 오프셋(`o:<n>`)이라 결정적이다.
"""

from __future__ import annotations

from orbit.connectors.contracts import (
    Address,
    Draft,
    DraftSpec,
    InvalidQuery,
    MailMessage,
    MailThread,
    MessageSummary,
    NotFound,
    SearchPage,
)
from orbit.connectors.sim.mail_query import matches, parse_query
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import AccountState, SimWorld

_SNIPPET_LEN = 100
_CURSOR_PREFIX = "o:"


def _snippet(body: str) -> str:
    return " ".join(body.split())[:_SNIPPET_LEN]


class SimMailConnector:
    def __init__(self, world: SimWorld, account_id: str, runtime: SimRuntime) -> None:
        self._world = world
        self._account_id = account_id
        self._rt = runtime
        self._state: AccountState = world.account(account_id)

    # ------------------------------------------------------------ 내부

    def _draft_message(self, draft: Draft) -> MailMessage:
        spec = draft.spec
        if spec is None:  # 스냅샷 로드와 create_draft가 spec을 보장한다
            raise NotFound(f"초안 {draft.draft_id}에 내용이 없다")
        return MailMessage(
            id=draft.message_id,
            thread_id=draft.thread_id,
            from_=Address(email=self._state.email),
            to=spec.to,
            cc=spec.cc,
            subject=spec.subject,
            body_text=spec.body_text,
            snippet=_snippet(spec.body_text),
            sent_at=self._world.clock.now(),
            labels=("DRAFT",),
            attachments=(),
            headers=spec.headers,
        )

    def _find_draft_message(self, message_id: str) -> MailMessage | None:
        for draft in self._state.drafts.values():
            if draft.message_id == message_id:
                return self._draft_message(draft)
        return None

    @staticmethod
    def _summary(msg: MailMessage) -> MessageSummary:
        return MessageSummary(
            id=msg.id,
            thread_id=msg.thread_id,
            from_=msg.from_,
            subject=msg.subject,
            snippet=msg.snippet,
            sent_at=msg.sent_at,
            labels=msg.labels,
            has_attachment=msg.has_attachment,
        )

    # ------------------------------------------------------------ 계약

    async def search_messages(
        self, query: str, *, max_results: int = 20, page_token: str | None = None
    ) -> SearchPage:
        args = {"query": query, "max_results": max_results, "page_token": page_token}
        return await self._rt.run_tool(
            "search_messages", args, lambda: self._search(query, max_results, page_token)
        )

    def _search(self, query: str, max_results: int, page_token: str | None) -> SearchPage:
        parsed = parse_query(query)
        if max_results < 1:
            raise InvalidQuery("max_results는 1 이상이어야 한다")
        offset = 0
        if page_token is not None:
            if not page_token.startswith(_CURSOR_PREFIX) or not (
                page_token[2:].isascii() and page_token[2:].isdigit()
            ):
                raise InvalidQuery(f"잘못된 page_token: {page_token!r}")
            offset = int(page_token[len(_CURSOR_PREFIX) :])
        email = self._state.email
        candidates = list(self._state.messages.values())
        if parsed.wants_drafts:
            candidates += [self._draft_message(d) for d in self._state.drafts.values()]
        found = [m for m in candidates if matches(parsed, m, email)]
        found.sort(key=lambda m: (m.sent_at, m.id), reverse=True)
        page = found[offset : offset + max_results]
        next_offset = offset + max_results
        token = f"{_CURSOR_PREFIX}{next_offset}" if next_offset < len(found) else None
        return SearchPage(messages=tuple(self._summary(m) for m in page), next_page_token=token)

    async def get_message(self, message_id: str) -> MailMessage:
        def apply() -> MailMessage:
            msg = self._state.messages.get(message_id)
            if msg is None:
                msg = self._find_draft_message(message_id)
            if msg is None:
                raise NotFound(f"메시지 {message_id}가 없다")
            return msg

        return await self._rt.run_tool("get_message", {"message_id": message_id}, apply)

    async def get_thread(self, thread_id: str) -> MailThread:
        def apply() -> MailThread:
            ids = self._state.threads.get(thread_id)
            if ids is None:
                raise NotFound(f"스레드 {thread_id}가 없다")
            msgs = sorted((self._state.messages[i] for i in ids), key=lambda m: (m.sent_at, m.id))
            return MailThread(id=thread_id, messages=tuple(msgs))

        return await self._rt.run_tool("get_thread", {"thread_id": thread_id}, apply)

    async def create_draft(self, spec: DraftSpec) -> Draft:
        def apply() -> Draft:
            if spec.reply_to_message_id is not None:
                origin = self._state.messages.get(spec.reply_to_message_id)
                if origin is None:
                    raise NotFound(f"답장 대상 메시지 {spec.reply_to_message_id}가 없다")
                thread_id = origin.thread_id
            else:
                thread_id = ""
            ids = self._world.ids
            draft_id = ids.draft_id()
            message_id = ids.message_id()
            if not thread_id:
                thread_id = ids.thread_id()
            draft = Draft(draft_id=draft_id, message_id=message_id, thread_id=thread_id, spec=spec)
            self._state.drafts[draft_id] = draft
            self._state.record_draft("created", draft)
            return draft

        return await self._rt.run_tool("create_draft", {"spec": spec}, apply)

    async def get_draft(self, draft_id: str) -> Draft:
        def apply() -> Draft:
            draft = self._state.drafts.get(draft_id)
            if draft is None:
                raise NotFound(f"초안 {draft_id}가 없다(없거나 발송·삭제됨)")
            return draft

        return await self._rt.run_tool("get_draft", {"draft_id": draft_id}, apply)

    async def send_draft(self, draft_id: str) -> MailMessage:
        def apply() -> MailMessage:
            draft = self._state.drafts.get(draft_id)
            if draft is None or draft.spec is None:
                raise NotFound(f"초안 {draft_id}가 없다(없거나 발송·삭제됨)")
            spec = draft.spec
            email = self._state.email.lower()
            to_self = any(a.email.lower() == email for a in spec.to + spec.cc)
            labels = ("SENT", "INBOX") if to_self else ("SENT",)
            message = MailMessage(
                id=self._world.ids.message_id(),
                thread_id=draft.thread_id,
                from_=Address(email=self._state.email),
                to=spec.to,
                cc=spec.cc,
                subject=spec.subject,
                body_text=spec.body_text,
                snippet=_snippet(spec.body_text),
                sent_at=self._world.clock.now(),
                labels=labels,
                attachments=(),
                headers=spec.headers,
            )
            del self._state.drafts[draft_id]
            self._state.messages[message.id] = message
            self._state.threads.setdefault(draft.thread_id, []).append(message.id)
            self._state.record_draft("sent", draft, sent_message_id=message.id)
            return message

        return await self._rt.run_tool("send_draft", {"draft_id": draft_id}, apply)

    async def delete_draft(self, draft_id: str) -> None:
        def apply() -> None:
            if draft_id not in self._state.drafts:
                raise NotFound(f"초안 {draft_id}가 없다(없거나 발송·삭제됨)")
            self._state.record_draft("deleted", self._state.drafts.pop(draft_id))

        await self._rt.run_tool("delete_draft", {"draft_id": draft_id}, apply)
