import { PRODUCT_NAME } from "../brand";
import { executionResultText } from "../approvals/logic";
import type { ChatMessage, ExecutionOutcome } from "../runs/reducer";
import { Icon, type IconName } from "../ui/Icon";
import type { ComposerStatus } from "../composer/useComposer";
import { Markdown } from "./Markdown";

function resultIcon(status: ExecutionOutcome["status"]): { icon: IconName; tone: string } {
  switch (status) {
    case "succeeded":
      return { icon: "success", tone: "success" };
    case "failed":
      return { icon: "error", tone: "error" };
    case "needs_user":
      return { icon: "warning", tone: "warning" };
    default:
      return { icon: "info", tone: "info" };
  }
}

/** 대화 목록. 화면낭독기는 새 항목이 추가될 때 문장 단위로 읽는다. */
export function MessageList({
  messages,
  outgoing,
  sendStatus,
  results,
  working,
}: {
  messages: ChatMessage[];
  /** 아직 서버에 접수되지 않은 사용자 메시지 */
  outgoing: string | null;
  sendStatus: ComposerStatus;
  results: ExecutionOutcome[];
  /** 응답을 기다리는 중이면 자리를 미리 잡아 둔다 */
  working: boolean;
}) {
  const empty = messages.length === 0 && outgoing === null;
  return (
    <ol className="messages" role="log" aria-label="대화">
      {empty && (
        <li className="messages-empty">
          <p className="display-ko messages-empty-title" aria-hidden="true">
            새 대화
          </p>
          <p className="messages-empty-note">
            메일, 일정, 문서에 대해 물어보세요. 예: “내일 일정 알려 줘”
          </p>
        </li>
      )}
      {messages.map((message) =>
        message.role === "notice" ? (
          <li key={message.id} className="message-notice">
            <Icon name="info" />
            <span>{message.text}</span>
          </li>
        ) : (
          <li key={message.id} className={`message message-${message.role}`}>
            {message.role === "user" ? (
              <span className="message-tag tag">나</span>
            ) : (
              <span className="message-tag tag tag-mono">{PRODUCT_NAME}</span>
            )}
            {message.role === "assistant" ? (
              <Markdown text={message.text} />
            ) : (
              <p>{message.text}</p>
            )}
          </li>
        ),
      )}
      {outgoing !== null && (
        <li className="message message-user message-unsent" data-send-status={sendStatus}>
          <span className="message-tag tag">나</span>
          <p>{outgoing}</p>
          {sendStatus === "failed" ? (
            <span className="message-meta label">
              <Icon name="error" />
              <span>전송하지 못했습니다</span>
            </span>
          ) : (
            <span className="message-meta label">전송 중</span>
          )}
        </li>
      )}
      {results.map((result) => {
        const { icon, tone } = resultIcon(result.status);
        return (
          <li key={result.approvalId} className={`note tone-${tone}`}>
            <Icon name={icon} />
            <span>{executionResultText(result.status, result.toolName)}</span>
          </li>
        );
      })}
      {working && (
        <li className="message message-assistant message-working" aria-hidden="true">
          <span className="loading-dots">
            <span />
            <span />
            <span />
          </span>
        </li>
      )}
    </ol>
  );
}
