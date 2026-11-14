import { useCallback, useEffect, useRef, useState } from "react";
import { createRun, getConversation } from "../api/endpoints";
import { isApiError } from "../api/errors";
import { PRODUCT_NAME } from "../brand";
import { ApprovalPanel } from "../approvals/ApprovalPanel";
import { useConnectionNotice } from "../connections/useConnections";
import { NotificationCenter } from "../notifications/NotificationCenter";
import type { NotificationsApi } from "../notifications/useNotifications";
import { useComposer } from "../composer/useComposer";
import { CONVERSATION_PANEL_ID, ConversationPanel } from "../conversations/ConversationPanel";
import { turnsToView } from "../conversations/turns";
import { useConversationList } from "../conversations/useConversationList";
import { useMediaQuery, WIDE_LAYOUT_QUERY } from "../conversations/useMediaQuery";
import { useRunStream } from "../runs/useRunStream";
import { Icon } from "../ui/Icon";
import { Logo } from "../ui/Logo";
import { Composer } from "./Composer";
import { useIdempotencyKey } from "./idempotency";
import { MessageList } from "./MessageList";
import { StatusBar } from "./StatusBar";
import { connectionStatusItem, connectorNoticeItems, runStatusItem } from "./status";
import { useStickToBottom } from "./useStickToBottom";
import "./chat.css";

const BUSY_TEXT = "이 대화에서 아직 진행 중인 작업이 있습니다. 끝난 뒤 다시 보내 주세요.";
const KEY_REUSED_TEXT = "같은 요청을 다시 보내지 못했습니다. 새로 보내 주세요.";

function loadFailureText(error: unknown): string {
  return isApiError(error) && error.kind === "not_found"
    ? "이 대화를 찾을 수 없습니다. 목록을 새로 불러온 뒤 다시 선택해 주세요."
    : "대화를 불러오지 못했습니다. 연결을 확인한 뒤 다시 선택해 주세요.";
}

export function ChatScreen({
  onSignOut,
  onOpenConnections,
  onStreamClosed,
  notifications,
}: {
  onSignOut: () => void;
  onOpenConnections: () => void;
  /** 도착한 알림 상태(ADR-0021). 있으면 헤더에 알림 버튼을 보인다. */
  notifications?: NotificationsApi;
  /** 실시간 연결이 끊긴 채 닫혔을 때 부른다. 세션이 끝났는지 다시 확인하는 데 쓴다. */
  onStreamClosed?: () => void;
}) {
  const connectionNotice = useConnectionNotice();
  const [runId, setRunId] = useState<string | null>(null);
  const [outgoing, setOutgoing] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | undefined>(undefined);
  const conversationIdRef = useRef<string | undefined>(undefined);
  const messageCounter = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { state, connection, addUserMessage, replaceMessages } = useRunStream(runId);
  const idempotency = useIdempotencyKey();
  const [sendConflict, setSendConflict] = useState<"busy" | "key_reused" | null>(null);

  const wide = useMediaQuery(WIDE_LAYOUT_QUERY);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // 화면이 넓어지면 대화 상자 상태를 접는다(다시 좁아질 때 저절로 열리지 않게).
  if (wide && drawerOpen) setDrawerOpen(false);
  const drawerShown = drawerOpen && !wide;
  const toggleRef = useRef<HTMLButtonElement>(null);
  const list = useConversationList();
  const refreshList = list.refresh;
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const loadTicket = useRef(0);

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
  }, []);
  // 대화 상자를 닫으면 열었던 버튼으로 포커스를 돌려준다(항목을 골라 닫을 때는 입력창이 받는다).
  const returnFocus = useRef<"toggle" | "input">("toggle");
  const wasShown = useRef(false);
  useEffect(() => {
    if (wasShown.current && !drawerShown) {
      // 넓어져서 닫힌 경우 토글 버튼이 사라졌으므로 입력창이 포커스를 받는다.
      const target =
        returnFocus.current === "toggle" && !wide ? toggleRef.current : inputRef.current;
      target?.focus();
    }
    wasShown.current = drawerShown;
  }, [drawerShown, wide]);

  // 브라우저가 재접속을 포기하면(세션 만료·폐기로 서버가 닫은 경우 401) 세션을 다시 확인한다.
  const streamClosed = connection === "closed" && !state.finished;
  // 콜백이 바뀌어도 닫힘 전환당 한 번만 확인하도록 최신 콜백은 ref로 읽는다.
  const onStreamClosedRef = useRef(onStreamClosed);
  useEffect(() => {
    onStreamClosedRef.current = onStreamClosed;
  });
  useEffect(() => {
    if (streamClosed) onStreamClosedRef.current?.();
  }, [streamClosed]);

  const send = useCallback(
    async (text: string) => {
      setOutgoing(text);
      setSendConflict(null);
      // 응답이 유실돼 같은 내용을 다시 보내도 서버가 같은 run으로 알아보게 같은 키를 쓴다.
      const key = idempotency.keyFor(text);
      let created;
      try {
        created = await createRun(
          {
            message: text,
            ...(conversationIdRef.current === undefined
              ? {}
              : { conversation_id: conversationIdRef.current }),
          },
          undefined,
          key,
        );
      } catch (error) {
        if (isApiError(error) && error.kind === "conflict") {
          if (error.detail === "conversation_busy") {
            // 서버는 이 요청을 접수하지 않았다. 같은 키로 다시 보내도 되므로 키는 남긴다.
            setSendConflict("busy");
            refreshList();
          } else {
            idempotency.discard();
            setSendConflict("key_reused");
          }
        }
        throw error;
      }
      idempotency.discard();
      conversationIdRef.current = created.conversation_id;
      setConversationId(created.conversation_id);
      refreshList();
      messageCounter.current += 1;
      addUserMessage(`user-${messageCounter.current}`, text);
      setRunId(created.run_id);
      setOutgoing(null);
    },
    [addUserMessage, idempotency, refreshList],
  );
  const composer = useComposer(send);
  const sending = composer.status === "sending";

  /** 화면을 다른 대화(또는 빈 새 대화)로 바꾼다. 이전 대화의 실시간 구독은 `runId`가 바뀌며 닫힌다. */
  const switchConversation = useCallback(
    (
      id: string | undefined,
      messages: Parameters<typeof replaceMessages>[0],
      resume: string | null,
    ) => {
      conversationIdRef.current = id;
      setConversationId(id);
      replaceMessages(messages);
      setRunId(resume);
      setOutgoing(null);
      setSendConflict(null);
      setLoadError(null);
      idempotency.discard();
    },
    [replaceMessages, idempotency],
  );

  const startNewConversation = useCallback(() => {
    loadTicket.current += 1;
    setLoading(false);
    switchConversation(undefined, [], null);
    returnFocus.current = "input";
    setDrawerOpen(false);
    inputRef.current?.focus();
  }, [switchConversation]);

  const selectConversation = useCallback(
    async (id: string) => {
      returnFocus.current = "input";
      if (id === conversationIdRef.current) {
        setDrawerOpen(false);
        return;
      }
      const ticket = ++loadTicket.current;
      setLoading(true);
      setLoadError(null);
      try {
        const detail = await getConversation(id);
        if (ticket !== loadTicket.current) return; // 그 사이 다른 대화를 골랐다
        const view = turnsToView(detail.turns);
        switchConversation(id, view.messages, view.resumeRunId);
        setDrawerOpen(false);
      } catch (error) {
        if (ticket !== loadTicket.current) return;
        setLoadError(loadFailureText(error));
      } finally {
        if (ticket === loadTicket.current) setLoading(false);
      }
    },
    [switchConversation],
  );

  // run이 끝나면 목록의 마지막 활동 시각과 상태를 갱신한다.
  useEffect(() => {
    if (state.finished) refreshList();
  }, [state.finished, refreshList]);
  const busy = state.status === "queued" || state.status === "running";
  const approvalId = state.pendingApproval?.approvalId ?? null;
  const approvalWait = state.status === "waiting_approval" || approvalId !== null;

  const { scrollRef, contentRef, onScroll, showJump, jumpToBottom } = useStickToBottom(
    `${state.messages.length}:${state.results.length}:${outgoing ?? ""}:${busy}:${approvalId ?? ""}`,
    approvalId !== null, // 승인 카드를 위에서부터 읽을 수 있게 맨 아래로 끌어내리지 않는다
  );

  // 승인 카드가 사라지면(결정·만료·실행 완료) 포커스가 문서 맨 위로 날아가지 않게 입력창으로 돌린다.
  const hadApproval = useRef(false);
  useEffect(() => {
    if (approvalId === null && hadApproval.current && document.activeElement === document.body) {
      inputRef.current?.focus();
    }
    hadApproval.current = approvalId !== null;
  }, [approvalId]);

  return (
    <div className="chat-shell">
      {(wide || drawerShown) && (
        <ConversationPanel
          layout={wide ? "wide" : "drawer"}
          list={list.state}
          currentId={conversationId}
          busy={sending || loading}
          loadError={loadError}
          onNew={startNewConversation}
          onSelect={(id) => void selectConversation(id)}
          onRetry={list.retry}
          onClose={closeDrawer}
        />
      )}
      <main className="chat paper-texture" inert={drawerShown}>
        <h1 className="visually-hidden">{PRODUCT_NAME} 대화</h1>
        <header className="chat-header">
          <div className="chat-header-start">
            <Logo className="chat-logo" />
            {!wide && (
              <button
                type="button"
                ref={toggleRef}
                className="btn btn-ink"
                aria-expanded={drawerShown}
                aria-controls={drawerShown ? CONVERSATION_PANEL_ID : undefined}
                onClick={() => {
                  returnFocus.current = "toggle";
                  setDrawerOpen(true);
                }}
              >
                대화 목록
              </button>
            )}
          </div>
          <div className="chat-header-actions">
            {notifications !== undefined && <NotificationCenter notifications={notifications} />}
            <button type="button" className="btn btn-secondary" onClick={onOpenConnections}>
              계정 연결
            </button>
            <button type="button" className="btn btn-ink" onClick={onSignOut}>
              로그아웃
            </button>
          </div>
        </header>

        {/* 영역은 항상 그려 두고 내용만 바꿔야 화면낭독기가 변화를 읽는다. */}
        <div role="status" className="connection-banner-slot">
          {connectionNotice === "needs_connection" && (
            <div className="connection-banner crop">
              <p className="note tone-warning">
                <Icon name="warning" />
                <span>
                  Google 계정이 연결되지 않아 메일과 일정을 다룰 수 없습니다. 먼저 계정을 연결해
                  주세요.
                </span>
              </p>
              <button type="button" className="btn btn-primary" onClick={onOpenConnections}>
                계정 연결하기
              </button>
            </div>
          )}
        </div>

        <div className="chat-body">
          <div
            className="chat-scroll"
            ref={scrollRef}
            onScroll={onScroll}
            role="region"
            aria-label="대화 내용"
            tabIndex={0}
          >
            <div className="chat-column" ref={contentRef}>
              <MessageList
                messages={state.messages}
                outgoing={outgoing}
                sendStatus={composer.status}
                results={state.results}
                working={busy}
              />
              {approvalId !== null && <ApprovalPanel key={approvalId} approvalId={approvalId} />}
            </div>
          </div>
          {showJump && approvalId === null && (
            <button type="button" className="btn btn-ink jump" onClick={jumpToBottom}>
              맨 아래로
            </button>
          )}
        </div>

        <footer className="chat-footer">
          <div className="chat-column">
            <StatusBar
              items={[
                runStatusItem(state),
                ...connectorNoticeItems(state.connectorNotices),
                connectionStatusItem(connection, state.finished),
              ]}
            />
            <Composer
              composer={composer}
              inputRef={inputRef}
              blocked={approvalWait || loading}
              {...(loading ? { blockedHint: "대화를 불러오는 중입니다." } : {})}
              {...(sendConflict === "busy" ? { failureText: BUSY_TEXT } : {})}
              {...(sendConflict === "key_reused" ? { failureText: KEY_REUSED_TEXT } : {})}
            />
          </div>
        </footer>
      </main>
    </div>
  );
}
