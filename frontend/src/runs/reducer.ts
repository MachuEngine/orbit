import type { ExecutionStatus, RunStatus } from "../api/schemas";
import { isTerminalStatus } from "../api/schemas";
import type { RunEvent } from "./events";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
}

export interface PendingApproval {
  approvalId: string;
  toolName: string;
  summary: Record<string, unknown>;
}

export interface ExecutionOutcome {
  approvalId: string;
  status: ExecutionStatus;
  errorKind: string | null;
}

export interface RunViewState {
  /** 마지막으로 반영한 seq. 같거나 작은 seq의 이벤트는 무시한다. */
  lastSeq: number;
  status: RunStatus | "idle";
  messages: ChatMessage[];
  pendingApproval: PendingApproval | null;
  results: ExecutionOutcome[];
  /** 서버가 알린 run 오류 종류 */
  error: string | null;
  /** 스트림 자체의 문제(형태가 다른 이벤트 등) */
  streamProblem: "invalid_event" | null;
  /** 종료 상태를 받아 더 이상 이벤트가 오지 않는다 */
  finished: boolean;
}

export type RunViewAction =
  | { type: "run_started" }
  | { type: "user_message"; id: string; text: string }
  | { type: "event"; event: RunEvent }
  | { type: "invalid_event" };

export const initialRunViewState: RunViewState = {
  lastSeq: 0,
  status: "idle",
  messages: [],
  pendingApproval: null,
  results: [],
  error: null,
  streamProblem: null,
  finished: false,
};

export function runViewReducer(state: RunViewState, action: RunViewAction): RunViewState {
  switch (action.type) {
    case "run_started":
      // 새 run을 시작한다. 대화 메시지는 유지하고 run 단위 상태만 비운다.
      return { ...initialRunViewState, messages: state.messages };
    case "user_message":
      return {
        ...state,
        messages: [...state.messages, { id: action.id, role: "user", text: action.text }],
      };
    case "invalid_event":
      return { ...state, streamProblem: "invalid_event" };
    case "event":
      return applyEvent(state, action.event);
  }
}

function applyEvent(state: RunViewState, event: RunEvent): RunViewState {
  if (event.seq <= state.lastSeq) return state; // 재접속 재생으로 온 중복
  const next: RunViewState = { ...state, lastSeq: event.seq };
  switch (event.type) {
    case "status": {
      const status = event.payload.status;
      return {
        ...next,
        status,
        // 승인 대기가 끝나면 대기 중 승인을 비운다.
        pendingApproval: status === "waiting_approval" ? next.pendingApproval : null,
        finished: isTerminalStatus(status),
      };
    }
    case "error":
      return { ...next, error: event.payload.error_kind };
    case "assistant_text":
      return {
        ...next,
        messages: [
          ...next.messages,
          { id: `assistant-${event.seq}`, role: "assistant", text: event.payload.text },
        ],
      };
    case "approval_requested":
      return {
        ...next,
        pendingApproval: {
          approvalId: event.payload.approval_id,
          toolName: event.payload.tool_name,
          summary: event.payload.summary,
        },
      };
    case "execution_result": {
      const { approval_id: approvalId, status, error_kind: errorKind } = event.payload;
      return {
        ...next,
        pendingApproval:
          next.pendingApproval?.approvalId === approvalId ? null : next.pendingApproval,
        results: [...next.results, { approvalId, status, errorKind: errorKind ?? null }],
      };
    }
  }
}
