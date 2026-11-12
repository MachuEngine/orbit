import type { ConnectorNotice, RunViewState } from "../runs/reducer";
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

const LLM_ERROR_KIND = /^(\w*LLM\w*|ModelMismatchError|MalformedResponseError)$/;

/** 서버가 알린 실패 종류를 사용자가 이해하는 말로 바꾼다. 모르는 종류는 일반 문구다. */
export function failureText(errorKind: string | null): string {
  if (errorKind === "no_connection") {
    return "Google 계정이 연결되어 있지 않습니다. 연결 화면에서 계정을 연결해 주세요.";
  }
  if (errorKind === "max_steps") {
    return "처리 단계가 너무 많아 중단했습니다. 요청을 나눠서 다시 보내 주세요.";
  }
  if (errorKind !== null && LLM_ERROR_KIND.test(errorKind)) {
    return "모델 호출에 실패했습니다. 잠시 뒤 다시 요청해 주세요.";
  }
  return "작업을 마치지 못했습니다. 잠시 뒤 다시 요청해 주세요.";
}

const SERVICE_NAME = { calendar: "Google 캘린더", mail: "Gmail", drive: "Google Drive" } as const;
const SAFE_REASON = /^[A-Za-z0-9_]{1,40}$/;

/** 커넥터 알림 문구. 서버가 준 reason은 안전한 코드 형태일 때만 덧붙인다(외부 문자열 노출 방지). */
export function connectorNoticeText(notice: ConnectorNotice): string {
  const name = SERVICE_NAME[notice.service];
  switch (notice.kind) {
    case "service_disabled":
      return `${name} API가 사용 설정되지 않아 정보를 가져오지 못했습니다. Google Cloud 콘솔에서 API를 사용 설정한 뒤 다시 시도해 주세요.`;
    case "quota_exceeded":
      return `${name} 사용 한도를 넘었습니다. 잠시 뒤 다시 시도해 주세요.`;
    case "rate_limited":
      return `${name} 요청이 많아 잠시 거절되었습니다. 잠시 뒤 다시 시도해 주세요.`;
    case "scope_missing":
      return "문서 권한이 없습니다. 계정 연결 화면에서 문서 권한을 추가해 주세요.";
    case "permission_revoked":
      return `${name} 접근 권한이 없습니다. 연결 화면에서 Google 계정을 다시 연결해 주세요.`;
    case "transient_error":
      return `${name}에 일시적으로 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.`;
    default:
      return SAFE_REASON.test(notice.reason)
        ? `${name}에서 오류가 발생했습니다. (오류 코드: ${notice.reason})`
        : `${name}에서 오류가 발생했습니다.`;
  }
}

export function connectorNoticeItems(notices: ConnectorNotice[]): StatusItem[] {
  return notices.map((notice) => ({
    id: `notice-${notice.service}-${notice.kind}`,
    tone: "warning",
    icon: "warning",
    text: connectorNoticeText(notice),
  }));
}

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
      text: failureText(run.error),
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
        text: "승인 대기 중입니다. 위 카드에서 실행 여부를 정하기 전에는 새 메시지를 전송할 수 없습니다.",
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
