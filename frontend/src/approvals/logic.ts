import type { Approval, ExecutionStatus, Provenance } from "../api/schemas";

/** 시각을 주입할 수 있게 한 시계. 테스트에서 고정 시각을 쓴다. */
export interface Clock {
  now: () => Date;
}

export const systemClock: Clock = { now: () => new Date() };

/** 만료까지 남은 밀리초. 이미 지났거나 시각이 잘못됐으면 0이다. */
export function remainingMs(expiresAt: string, clock: Clock = systemClock): number {
  const expires = Date.parse(expiresAt);
  if (Number.isNaN(expires)) return 0;
  return Math.max(0, expires - clock.now().getTime());
}

export function isExpired(expiresAt: string, clock: Clock = systemClock): boolean {
  return remainingMs(expiresAt, clock) === 0;
}

/** 남은 시간을 "3분 20초"처럼 쓴다. 0이면 "만료됨". */
export function formatRemaining(ms: number): string {
  if (ms <= 0) return "만료됨";
  const totalSeconds = Math.ceil(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours}시간`);
  if (minutes > 0) parts.push(`${minutes}분`);
  if (seconds > 0 && hours === 0) parts.push(`${seconds}초`);
  return parts.join(" ");
}

const WARNED: readonly Provenance[] = ["external_content", "model"];

export interface ProvenanceWarning {
  email: string;
  provenance: "external_content" | "model";
}

/** 사용자가 말하지 않았고 워크스페이스 데이터에도 없는 값(메일·문서 본문, 모델 생성)을 골라낸다. */
export function provenanceWarnings(provenance: Approval["provenance"]): ProvenanceWarning[] {
  const warnings: ProvenanceWarning[] = [];
  for (const [email, source] of Object.entries(provenance)) {
    if (source === "external_content" || source === "model") {
      warnings.push({ email, provenance: source });
    }
  }
  return warnings;
}

export function isWarnedProvenance(source: Provenance): boolean {
  return WARNED.includes(source);
}

export interface ActionLabel {
  /** 카드 제목 */
  name: string;
  /** 승인 버튼 문구 */
  confirm: string;
  /** 서버가 실행을 확인한 뒤의 알림 */
  done: string;
  /** 실행하지 못했다고 확인된 뒤의 알림 */
  failed: string;
}

/** 승인 버튼과 결과 알림에 쓰는 동작 문구. 모르는 도구는 null이라 호출자가 결정한다. */
export function actionLabel(toolName: string): ActionLabel | null {
  switch (toolName) {
    case "send_mail":
      return {
        name: "메일 보내기",
        confirm: "보내기",
        done: "메일을 보냈습니다.",
        failed: "메일을 보내지 못했습니다.",
      };
    case "create_event":
      return {
        name: "일정 만들기",
        confirm: "일정 만들기",
        done: "일정을 만들었습니다.",
        failed: "일정을 만들지 못했습니다.",
      };
    case "update_event_time":
      return {
        name: "일정 시간 변경",
        confirm: "일정 변경",
        done: "일정 시간을 변경했습니다.",
        failed: "일정 시간을 변경하지 못했습니다.",
      };
    case "cancel_event":
      return {
        name: "일정 취소",
        confirm: "일정 취소",
        done: "일정을 취소했습니다.",
        failed: "일정을 취소하지 못했습니다.",
      };
    default:
      return null;
  }
}

/** 이미 결정된 승인의 상태 문구. */
export function approvalStatusText(status: Approval["status"]): string {
  switch (status) {
    case "pending":
      return "";
    case "approved":
      return "이미 승인된 요청입니다.";
    case "rejected":
      return "실행하지 않기로 결정한 요청입니다.";
    case "expired":
      return "승인 시간이 지나 실행하지 않았습니다.";
  }
}

/** 실행 결과(execution_result) 안내 문구. 동작을 알면 승인 버튼과 같은 동사를 쓴다. */
export function executionResultText(status: ExecutionStatus, toolName?: string | null): string {
  const label = toolName === undefined || toolName === null ? null : actionLabel(toolName);
  switch (status) {
    case "succeeded":
      return label?.done ?? "요청한 작업을 실행했습니다.";
    case "rejected":
      return "실행하지 않았습니다.";
    case "expired":
      return "승인 시간이 지나 실행하지 않았습니다.";
    case "failed":
      return label?.failed ?? "작업을 실행하지 못했습니다.";
    case "needs_user":
      return "실행 결과를 확인하지 못했습니다. 받은편지함·일정을 직접 확인해 주세요.";
    case "pending":
    case "unknown":
      return "실행 결과를 확인하고 있습니다.";
  }
}

/** 되돌릴 수 없는 동작인지. 메일 발송만 해당한다. */
export function isIrreversible(toolName: string): boolean {
  return toolName === "send_mail";
}
