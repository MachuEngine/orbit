import type { Approval, Provenance } from "../api/schemas";

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

/** 승인 버튼과 결과 알림에 쓰는 동작 문구. 모르는 도구는 null이라 호출자가 결정한다. */
export function actionLabel(toolName: string): { name: string; confirm: string } | null {
  switch (toolName) {
    case "send_mail":
      return { name: "메일 보내기", confirm: "보내기" };
    case "create_event":
      return { name: "일정 만들기", confirm: "일정 만들기" };
    case "update_event_time":
      return { name: "일정 시간 변경", confirm: "일정 변경" };
    case "cancel_event":
      return { name: "일정 취소", confirm: "일정 취소" };
    default:
      return null;
  }
}

/** 되돌릴 수 없는 동작인지. 메일 발송만 해당한다. */
export function isIrreversible(toolName: string): boolean {
  return toolName === "send_mail";
}
