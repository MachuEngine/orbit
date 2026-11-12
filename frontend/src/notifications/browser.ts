import type { NotificationItem } from "../api/schemas";
import { browserBody } from "./text";

/** 이 시간보다 오래전에 도착한 알림은 브라우저 알림으로 다시 띄우지 않는다. */
export const BROWSER_NOTIFY_WINDOW_MS = 10 * 60_000;

export type BrowserPermission = "unsupported" | "default" | "granted" | "denied";

/** 이 브라우저의 알림 권한 상태. `Notification`이 없으면 `unsupported`다. */
export function browserPermission(): BrowserPermission {
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.permission;
}

/** 사용자가 버튼을 눌렀을 때만 부른다. 권한을 묻고 결과 상태를 돌려준다. */
export async function requestBrowserPermission(): Promise<BrowserPermission> {
  if (typeof Notification === "undefined") return "unsupported";
  try {
    return await Notification.requestPermission();
  } catch {
    return browserPermission();
  }
}

/** 새로 도착해 읽지 않았고, 도착한 지 10분이 안 됐고, 이 탭에서 아직 띄우지 않은 알림. */
export function selectToNotify(
  items: readonly NotificationItem[],
  shown: ReadonlySet<string>,
  now: number,
): NotificationItem[] {
  return items.filter((item) => {
    if (item.read || shown.has(item.id)) return false;
    const age = now - new Date(item.due_at).getTime();
    return age >= 0 && age <= BROWSER_NOTIFY_WINDOW_MS;
  });
}

/**
 * 브라우저 알림을 띄운다. 권한이 허용돼 있을 때만 띄우고 띄웠으면 true다.
 * `tag`를 알림 ID로 두어 탭이 여러 개여도 같은 알림은 한 번만 보인다. 제목은 문자열 그대로 넘긴다.
 */
export function showBrowserNotification(item: NotificationItem): boolean {
  if (browserPermission() !== "granted") return false;
  try {
    new Notification(item.title, { tag: item.id, body: browserBody(item.kind) });
    return true;
  } catch {
    return false; // 일부 모바일 브라우저는 생성자를 막는다. 목록에는 그대로 남는다.
  }
}
