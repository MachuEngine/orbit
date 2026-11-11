import type { RunViewState } from "../runs/reducer";
import type { ConnectionState } from "../runs/useRunStream";
import type { IconName } from "../ui/Icon";

export type StatusTone = "info" | "success" | "warning" | "error";

export interface StatusItem {
  id: string;
  tone: StatusTone;
  icon: IconName;
  text: string;
}

type RunProgress = Pick<RunViewState, "status" | "error" | "streamProblem">;

/** 실행 진행 상태를 사용자가 이해하는 말로 바꾼다. 표시할 것이 없으면 null이다. */
export function runStatusItem(run: RunProgress): StatusItem | null {
  if (run.streamProblem !== null) {
    return {
      id: "stream-problem",
      tone: "error",
      icon: "error",
      text: "서버 응답을 해석하지 못했습니다. 새로고침한 뒤 다시 시도해 주세요.",
    };
  }
  if (run.error !== null || run.status === "failed") {
    return {
      id: "failed",
      tone: "error",
      icon: "error",
      text: "작업을 마치지 못했습니다. 잠시 뒤 다시 요청해 주세요.",
    };
  }
  switch (run.status) {
    case "queued":
    case "running":
      return { id: "working", tone: "info", icon: "clock", text: "작업 중입니다." };
    case "waiting_approval":
      return {
        id: "waiting",
        tone: "warning",
        icon: "warning",
        text: "승인 대기 중입니다. 내용을 확인하고 실행 여부를 정해 주세요.",
      };
    case "succeeded":
      return { id: "done", tone: "success", icon: "success", text: "완료했습니다." };
    case "cancelled":
      return { id: "cancelled", tone: "info", icon: "info", text: "작업이 취소되었습니다." };
    case "idle":
      return null;
  }
}

/** 실시간 연결 상태. 정상이면 null이다. */
export function connectionStatusItem(
  connection: ConnectionState,
  finished: boolean,
): StatusItem | null {
  if (connection === "reconnecting") {
    return {
      id: "reconnecting",
      tone: "warning",
      icon: "warning",
      text: "연결이 끊겨 다시 연결하고 있습니다.",
    };
  }
  if (connection === "closed" && !finished) {
    return {
      id: "closed",
      tone: "error",
      icon: "error",
      text: "연결이 끊겼습니다. 새로고침해 주세요.",
    };
  }
  return null;
}
