import { PRODUCT_NAME } from "../brand";
import { executionResultText } from "../approvals/logic";
import type { ChatMessage, ExecutionOutcome } from "../runs/reducer";
import { Icon, type IconName } from "../ui/Icon";
import type { ComposerStatus } from "../composer/useComposer";

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
          메일, 일정, 문서에 대해 물어보세요. 예: “내일 일정 알려 줘”
        </li>
      )}
      {messages.map((message) => (
        <li key={message.id} className={`message message-${message.role}`}>
          <span className="visually-hidden">{message.role === "user" ? "나" : PRODUCT_NAME}</span>
          <p>{message.text}</p>
        </li>
      ))}
      {outgoing !== null && (
        <li className="message message-user message-unsent" data-send-status={sendStatus}>
          <span className="visually-hidden">나</span>
          <p>{outgoing}</p>
          <span className="message-meta">
            {sendStatus === "failed" ? "전송하지 못했습니다" : "전송 중"}
          </span>
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
          <span className="dots">
            <span />
            <span />
            <span />
          </span>
        </li>
      )}
    </ol>
  );
}
