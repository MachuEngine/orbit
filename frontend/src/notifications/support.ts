/** 알림 테스트 공통 도구. 데이터는 모두 합성 값이다. */
import { vi, type Mock } from "vitest";
import type { NotificationItem } from "../api/schemas";

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export const NOW = new Date("2026-11-12T10:00:00Z").getTime();

export function item(over: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: "n1",
    kind: "event_reminder",
    title: "합성 주간 회의",
    due_at: new Date(NOW - 60_000).toISOString(),
    event_start: new Date(NOW + 9 * 60_000).toISOString(),
    read: false,
    created_at: new Date(NOW - 120_000).toISOString(),
    ...over,
  };
}

export function arrivedBody(items: NotificationItem[], unread?: number) {
  return {
    notifications: items,
    unread_count: unread ?? items.filter((i) => !i.read).length,
  };
}

/** 테스트용 `Notification`. 만든 알림을 `instances`에 모은다. */
export interface FakeNotificationClass {
  new (title: string, options?: NotificationOptions): unknown;
  permission: NotificationPermission;
  requestPermission: Mock<() => Promise<NotificationPermission>>;
  instances: { title: string; options: NotificationOptions | undefined }[];
}

export function installNotification(permission: NotificationPermission): FakeNotificationClass {
  class Fake {
    static permission: NotificationPermission = permission;
    static requestPermission = vi.fn<() => Promise<NotificationPermission>>(() => {
      Fake.permission = "granted";
      return Promise.resolve("granted");
    });
    static instances: { title: string; options: NotificationOptions | undefined }[] = [];
    readonly title: string;
    constructor(title: string, options?: NotificationOptions) {
      this.title = title;
      Fake.instances.push({ title, options });
    }
  }
  vi.stubGlobal("Notification", Fake);
  return Fake;
}

/** `document.hidden`을 바꾸고 `visibilitychange`를 낸다. */
export function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event("visibilitychange"));
}

export function resetHidden(): void {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
}
