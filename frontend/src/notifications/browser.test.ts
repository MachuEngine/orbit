import { afterEach, describe, expect, it, vi } from "vitest";
import {
  BROWSER_NOTIFY_WINDOW_MS,
  browserPermission,
  requestBrowserPermission,
  selectToNotify,
  showBrowserNotification,
} from "./browser";
import { NOW, installNotification, item } from "./support";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("selectToNotify", () => {
  const minutesAgo = (n: number) => new Date(NOW - n * 60_000).toISOString();

  it("읽지 않았고 10분 안에 도착했고 아직 띄우지 않은 알림만 고른다", () => {
    const fresh = item({ id: "fresh", due_at: minutesAgo(1) });
    const edge = item({
      id: "edge",
      due_at: new Date(NOW - BROWSER_NOTIFY_WINDOW_MS).toISOString(),
    });
    const old = item({ id: "old", due_at: minutesAgo(11) });
    const read = item({ id: "read", due_at: minutesAgo(1), read: true });
    const shown = item({ id: "shown", due_at: minutesAgo(1) });
    const future = item({ id: "future", due_at: new Date(NOW + 60_000).toISOString() });
    const picked = selectToNotify([fresh, edge, old, read, shown, future], new Set(["shown"]), NOW);
    expect(picked.map((i) => i.id)).toEqual(["fresh", "edge"]);
  });
});

describe("브라우저 권한과 알림", () => {
  it("Notification이 없으면 unsupported이고 아무것도 띄우지 않는다", async () => {
    vi.stubGlobal("Notification", undefined);
    expect(browserPermission()).toBe("unsupported");
    expect(await requestBrowserPermission()).toBe("unsupported");
    expect(showBrowserNotification(item())).toBe(false);
  });

  it("권한이 default나 denied면 띄우지 않는다", () => {
    for (const permission of ["default", "denied"] as const) {
      const fake = installNotification(permission);
      expect(showBrowserNotification(item())).toBe(false);
      expect(fake.instances).toEqual([]);
    }
  });

  it("허용돼 있으면 제목은 문자열 그대로, tag는 알림 ID로 띄운다", () => {
    const fake = installNotification("granted");
    const hostile = item({ id: "n9", title: "<img src=x onerror=alert(1)> 합성" });
    expect(showBrowserNotification(hostile)).toBe(true);
    expect(fake.instances).toEqual([
      {
        title: "<img src=x onerror=alert(1)> 합성",
        options: { tag: "n9", body: "곧 시작하는 일정입니다." },
      },
    ]);
  });

  it("생성자가 막혀도 오류를 밖으로 내지 않는다", () => {
    const Blocked = Object.assign(
      function Blocked() {
        throw new TypeError("Illegal constructor");
      },
      { permission: "granted" },
    );
    vi.stubGlobal("Notification", Blocked);
    expect(showBrowserNotification(item())).toBe(false);
  });

  it("권한 요청은 결과 상태를 돌려준다", async () => {
    const fake = installNotification("default");
    expect(await requestBrowserPermission()).toBe("granted");
    expect(fake.requestPermission).toHaveBeenCalledTimes(1);
  });
});
