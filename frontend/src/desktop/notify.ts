import type { NotificationItem } from "../api/schemas";
import { browserBody } from "../notifications/text";
import { getDesktop } from "./bridge";

/** 셸(`desktop/src/shared/channels.ts`의 `LIMITS`)과 같은 상한. 셸도 같은 규칙으로 다시 정리한다. */
export const TITLE_MAX = 200;
export const BODY_MAX = 500;
export const FALLBACK_TITLE = "알림";

/**
 * 알림 문구를 셸이 거부하지 않게 정리한다. 제어 문자는 공백으로, 앞뒤 공백은 지우고, 상한을 넘으면
 * 말줄이고(서로게이트 쌍은 끊지 않는다), 비면 `fallback`을 쓴다.
 */
export function sanitizeText(value: string, max: number, fallback: string): string {
  // eslint-disable-next-line no-control-regex
  const text = value.replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim();
  if (text === "") return fallback;
  if (text.length <= max) return text;
  let end = max - 1;
  const last = text.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  return `${text.slice(0, end).trimEnd()}…`;
}

/**
 * 데스크톱이면 셸의 시스템 알림으로 보낸다. 보냈으면 true다.
 * 제목은 외부 문자열이라 본문에 넣지 않고 종류만 알린다(브라우저 알림과 같은 규칙).
 * 제목이 비었거나 길거나 제어 문자가 있어도 알림이 사라지지 않게 보내기 전에 정리한다.
 */
export function showDesktopNotification(item: NotificationItem): boolean {
  const desktop = getDesktop();
  if (desktop === undefined) return false;
  try {
    desktop.notify({
      id: item.id,
      title: sanitizeText(item.title, TITLE_MAX, FALLBACK_TITLE),
      body: sanitizeText(browserBody(item.kind), BODY_MAX, ""),
    });
    return true;
  } catch {
    return false;
  }
}
