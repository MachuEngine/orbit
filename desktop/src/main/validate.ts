import { LIMITS } from "../shared/channels";

export interface NotifyPayload {
  id: string;
  title: string;
  body: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 대체 제목. 제목이 비었거나 문자열이 아니면 쓴다. */
export const FALLBACK_TITLE = "알림";

/** 제어 문자(줄바꿈·탭 포함)를 공백으로 바꾼다. 알림 문구에 섞이면 안 된다. */
function stripControlChars(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ");
}

/** 상한(UTF-16 길이)을 넘으면 말줄임표를 붙여 자른다. 서로게이트 쌍을 가운데서 끊지 않는다. */
function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  let end = max - 1;
  const last = text.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  return `${text.slice(0, end).trimEnd()}…`;
}

/**
 * 알림 문구를 정리한다. 제어 문자는 공백으로, 앞뒤 공백은 지우고, 상한을 넘으면 말줄이고, 제목이
 * 비면 대체 문구를 쓴다. 검증 실패로 알림이 사라지지 않게 하려는 규칙이며 프론트와 같다.
 */
export function sanitizeText(value: unknown, max: number, fallback: string): string {
  const text = typeof value === "string" ? stripControlChars(value).trim() : "";
  return text === "" ? fallback : truncate(text, max);
}

/**
 * `notify` 인자를 검증·정리한다. `id`는 중복 제거 키라 바꾸지 않고, 형식이 다르거나 상한을 넘으면
 * null이다. 모르는 키가 있어도 거부한다. 제목·본문은 거부하지 않고 정리한다.
 */
export function parseNotifyPayload(value: unknown): NotifyPayload | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (keys.some((key) => key !== "id" && key !== "title" && key !== "body")) return null;
  const { id } = value;
  if (typeof id !== "string" || id.length < 1 || id.length > LIMITS.idMax) return null;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f-\u009f]/.test(id)) return null;
  return {
    id,
    title: sanitizeText(value.title, LIMITS.titleMax, FALLBACK_TITLE),
    body: sanitizeText(value.body, LIMITS.bodyMax, ""),
  };
}

/** `setBadge` 인자 검증. 0 이상 상한 이하의 정수만 허용한다. */
export function parseBadgeCount(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value)) return null;
  if (value < 0 || value > LIMITS.badgeMax) return null;
  return value;
}

export interface PrepareConnectArgs {
  intent: "connect" | "docs_scope";
}

/**
 * `prepareConnect` 인자 검증. `intent`만 확인한다(대상 연결 ID는 서버가 티켓·세션으로 확인하므로 main은
 * 쓰지 않는다). 모르는 키는 거부한다.
 */
export function parsePrepareConnectArgs(value: unknown): PrepareConnectArgs | null {
  if (!isRecord(value)) return null;
  if (Object.keys(value).some((key) => key !== "intent" && key !== "connection_id")) return null;
  const { intent, connection_id: connectionId } = value;
  if (intent !== "connect" && intent !== "docs_scope") return null;
  if (connectionId !== undefined && (typeof connectionId !== "string" || connectionId.length > 64))
    return null;
  return { intent };
}
