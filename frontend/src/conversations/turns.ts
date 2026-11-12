import { isTerminalStatus, type ConversationTurn } from "../api/schemas";
import type { ChatMessage } from "../runs/reducer";

export const FAILED_NOTICE = "이 요청은 처리하지 못했습니다.";
export const CANCELLED_NOTICE = "이 요청은 취소되었습니다.";

export interface ConversationView {
  messages: ChatMessage[];
  /** 마지막 턴이 끝나지 않았으면 그 run. 이어서 실시간 구독할 대상이다. */
  resumeRunId: string | null;
}

/**
 * 저장된 턴을 화면 메시지로 바꾼다.
 * 끝나지 않은 마지막 턴은 사용자 메시지만 채운다. 비서 응답과 승인 카드는 그 run의 이벤트를
 * 처음부터 다시 받아 채우므로, 여기서도 넣으면 같은 문장이 두 번 보인다.
 */
export function turnsToView(turns: ConversationTurn[]): ConversationView {
  const messages: ChatMessage[] = [];
  let resumeRunId: string | null = null;
  turns.forEach((turn, index) => {
    messages.push({ id: `turn-${turn.run_id}-user`, role: "user", text: turn.user_text });
    const open = !isTerminalStatus(turn.status) && index === turns.length - 1;
    if (open) {
      resumeRunId = turn.run_id;
      return;
    }
    const hasText = turn.assistant_text !== null && turn.assistant_text !== "";
    if (hasText) {
      messages.push({
        id: `turn-${turn.run_id}-assistant`,
        role: "assistant",
        text: turn.assistant_text ?? "",
      });
    }
    // 응답이 없는 실패·취소는 이유를 알려 준다. 응답이 일부 있어도 실패면 끝까지 못 했다고 덧붙인다.
    if (turn.status === "failed") {
      messages.push({ id: `turn-${turn.run_id}-notice`, role: "notice", text: FAILED_NOTICE });
    } else if (turn.status === "cancelled" && !hasText) {
      messages.push({ id: `turn-${turn.run_id}-notice`, role: "notice", text: CANCELLED_NOTICE });
    }
  });
  return { messages, resumeRunId };
}
