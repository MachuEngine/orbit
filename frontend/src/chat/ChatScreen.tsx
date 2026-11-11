import { useCallback, useEffect, useRef, useState } from "react";
import { createRun } from "../api/endpoints";
import { ApprovalPanel } from "../approvals/ApprovalPanel";
import { useConnectionNotice } from "../connections/useConnections";
import { useComposer } from "../composer/useComposer";
import { useRunStream } from "../runs/useRunStream";
import { Icon } from "../ui/Icon";
import { Logo } from "../ui/Logo";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { StatusBar } from "./StatusBar";
import { connectionStatusItem, runStatusItem } from "./status";
import { useStickToBottom } from "./useStickToBottom";
import "./chat.css";

export function ChatScreen({
  onSignOut,
  onOpenConnections,
}: {
  onSignOut: () => void;
  onOpenConnections: () => void;
}) {
  const connectionNotice = useConnectionNotice();
  const [runId, setRunId] = useState<string | null>(null);
  const [outgoing, setOutgoing] = useState<string | null>(null);
  const conversationId = useRef<string | undefined>(undefined);
  const messageCounter = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { state, connection, addUserMessage } = useRunStream(runId);

  const send = useCallback(
    async (text: string) => {
      // 미해결: 응답이 유실돼 사용자가 다시 보내면 같은 메시지로 run이 두 번 만들어질 수 있다.
      // 백엔드에 멱등 키가 없어 화면에서 막을 수 없다. 멱등 키 도입 후 요청에 붙인다.
      setOutgoing(text);
      const created = await createRun({
        message: text,
        ...(conversationId.current === undefined
          ? {}
          : { conversation_id: conversationId.current }),
      });
      conversationId.current = created.conversation_id;
      messageCounter.current += 1;
      addUserMessage(`user-${messageCounter.current}`, text);
      setRunId(created.run_id);
      setOutgoing(null);
    },
    [addUserMessage],
  );
  const composer = useComposer(send);
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
    <main className="chat">
      <header className="chat-header">
        <Logo className="chat-logo" />
        <div className="chat-header-actions">
          <button type="button" className="btn btn-secondary" onClick={onOpenConnections}>
            계정 연결
          </button>
          <button type="button" className="btn btn-secondary" onClick={onSignOut}>
            로그아웃
          </button>
        </div>
      </header>

      {/* 영역은 항상 그려 두고 내용만 바꿔야 화면낭독기가 변화를 읽는다. */}
      <div role="status" className="connection-banner-slot">
        {connectionNotice === "needs_connection" && (
          <div className="connection-banner">
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
          <button type="button" className="btn btn-secondary jump" onClick={jumpToBottom}>
            맨 아래로
          </button>
        )}
      </div>

      <footer className="chat-footer">
        <div className="chat-column">
          <StatusBar
            items={[runStatusItem(state), connectionStatusItem(connection, state.finished)]}
          />
          <Composer composer={composer} inputRef={inputRef} blocked={approvalWait} />
        </div>
      </footer>
    </main>
  );
}
