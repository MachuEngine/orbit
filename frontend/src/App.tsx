// 상태 계층을 연결하는 최소 마크업이다. 색·글꼴·크기는 브랜드 토큰이 정해진 뒤에 입힌다.
import { useCallback, useRef, useState } from "react";
import { ApprovalPanel } from "./approvals/ApprovalPanel";
import { createRun } from "./api/endpoints";
import { useComposer } from "./composer/useComposer";
import { useRunStream } from "./runs/useRunStream";
import { useSession } from "./session/useSession";

export function App() {
  const { state: session, signIn, signOut } = useSession();

  if (session.phase === "loading") return <p role="status">불러오는 중입니다.</p>;
  if (session.phase === "error") {
    return <p role="alert">서버에 연결하지 못했습니다. 잠시 뒤 새로고침해 주세요.</p>;
  }
  if (session.phase === "signed_out") {
    return (
      <main>
        <h1>Orbit</h1>
        <p>로그인이 필요합니다.</p>
        <button type="button" onClick={() => void signIn()}>
          개발용 로그인
        </button>
      </main>
    );
  }
  return <Chat onSignOut={() => void signOut()} />;
}

function Chat({ onSignOut }: { onSignOut: () => void }) {
  const [runId, setRunId] = useState<string | null>(null);
  const conversationId = useRef<string | undefined>(undefined);
  const messageCounter = useRef(0);
  const { state, connection, addUserMessage } = useRunStream(runId);

  const send = useCallback(
    async (text: string) => {
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
    },
    [addUserMessage],
  );
  const composer = useComposer(send);
  const busy = state.status === "queued" || state.status === "running";

  return (
    <main>
      <header>
        <h1>Orbit</h1>
        <button type="button" onClick={onSignOut}>
          로그아웃
        </button>
      </header>

      <section aria-label="대화">
        <ol role="log" aria-live="polite">
          {state.messages.map((message) => (
            <li key={message.id}>
              <strong>{message.role === "user" ? "나" : "Orbit"}</strong> {message.text}
            </li>
          ))}
        </ol>
        {busy && <p role="status">처리하고 있습니다.</p>}
        {connection === "reconnecting" && <p role="status">연결이 끊겨 다시 연결하고 있습니다.</p>}
        {connection === "closed" && !state.finished && (
          <p role="alert">연결이 끊겼습니다. 새로고침해 주세요.</p>
        )}
        {state.streamProblem !== null && <p role="alert">서버 응답을 해석하지 못했습니다.</p>}
        {state.error !== null && <p role="alert">실행에 실패했습니다. 다시 시도해 주세요.</p>}
        {state.results.map((result) => (
          <p key={result.approvalId} role="status">
            {result.status === "succeeded"
              ? "요청한 작업을 실행했습니다."
              : "요청한 작업을 끝내지 못했습니다. 상태를 확인해 주세요."}
          </p>
        ))}
      </section>

      {state.pendingApproval !== null && (
        <ApprovalPanel
          key={state.pendingApproval.approvalId}
          approvalId={state.pendingApproval.approvalId}
        />
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void composer.submit();
        }}
      >
        <label htmlFor="composer-input">메시지</label>
        <textarea id="composer-input" {...composer.inputProps} />
        <button type="submit" disabled={composer.status === "sending"}>
          {composer.status === "sending" ? "보내는 중" : "보내기"}
        </button>
        {composer.status === "failed" && (
          <p role="alert">
            메시지를 보내지 못했습니다.{" "}
            <button type="button" onClick={() => void composer.retry()}>
              다시 보내기
            </button>
          </p>
        )}
      </form>
    </main>
  );
}
