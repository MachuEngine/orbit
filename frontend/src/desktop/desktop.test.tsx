import { act, render, renderHook, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NotificationPanel } from "../notifications/NotificationPanel";
import { NOW, arrivedBody, installNotification, item, json } from "../notifications/support";
import { useNotifications, type NotificationsApi } from "../notifications/useNotifications";
import { VOICE_TEXT, useVoiceInput } from "../voice/useVoiceInput";
import { installFakeMic } from "../voice/voiceTestSupport";
import { getDesktop, isDesktop } from "./bridge";
import { useDesktopBadge, useDesktopVoiceToggle } from "./hooks";
import {
  BODY_MAX,
  FALLBACK_TITLE,
  TITLE_MAX,
  sanitizeText,
  showDesktopNotification,
} from "./notify";
import { installDesktop, type FakeDesktop } from "./support";

let desktop: FakeDesktop | undefined;

afterEach(() => {
  desktop?.uninstall();
  desktop = undefined;
  vi.unstubAllGlobals();
});

function stubFetch(handler: (url: string) => Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => Promise.resolve(handler(url))),
  );
}

describe("bridge", () => {
  it("브라우저(브리지 없음)에서는 데스크톱이 아니다", () => {
    expect(getDesktop()).toBeUndefined();
    expect(isDesktop()).toBe(false);
  });

  it("브리지가 있으면 데스크톱이다", () => {
    desktop = installDesktop();
    expect(getDesktop()).toBe(desktop.api);
    expect(isDesktop()).toBe(true);
  });

  it("형태가 다른 객체는 없는 것으로 본다", () => {
    (window as { orbitDesktop?: unknown }).orbitDesktop = { notify: "x" };
    expect(getDesktop()).toBeUndefined();
    delete window.orbitDesktop;
  });
});

describe("showDesktopNotification", () => {
  it("셸의 notify로 보내고 제목 대신 종류만 본문에 넣는다", () => {
    desktop = installDesktop();
    expect(showDesktopNotification(item({ id: "n7", title: "합성 회의" }))).toBe(true);
    expect(desktop.notify).toHaveBeenCalledWith({
      id: "n7",
      title: "합성 회의",
      body: "곧 시작하는 일정입니다.",
    });
  });

  it("브리지가 없으면 false이고 셸은 부르지 않는다", () => {
    expect(showDesktopNotification(item())).toBe(false);
  });

  it("셸 호출이 던지면 false다", () => {
    desktop = installDesktop();
    desktop.notify.mockImplementation(() => {
      throw new Error("ipc");
    });
    expect(showDesktopNotification(item())).toBe(false);
  });
});

describe("알림 문구 정리", () => {
  it("긴 제목은 상한 안으로 말줄인다", () => {
    desktop = installDesktop();
    showDesktopNotification(item({ title: "가".repeat(TITLE_MAX + 30) }));
    const sent = desktop.notify.mock.calls[0]?.[0];
    expect(sent?.title).toHaveLength(TITLE_MAX);
    expect(sent?.title.endsWith("…")).toBe(true);
  });

  it("빈 제목과 제어 문자만 있는 제목은 대체 문구로 보낸다", () => {
    desktop = installDesktop();
    showDesktopNotification(item({ id: "a", title: "" }));
    showDesktopNotification(item({ id: "b", title: "\u0000\n" }));
    expect(desktop.notify.mock.calls.map(([p]) => p.title)).toEqual([
      FALLBACK_TITLE,
      FALLBACK_TITLE,
    ]);
  });

  it("제어 문자는 공백으로 바꾸고 보낸다", () => {
    desktop = installDesktop();
    expect(showDesktopNotification(item({ title: "합성\u0000회의\n2" }))).toBe(true);
    expect(desktop.notify.mock.calls[0]?.[0].title).toBe("합성 회의 2");
  });

  it("상한 길이와 서로게이트 경계를 지킨다", () => {
    const exact = "가".repeat(TITLE_MAX);
    expect(sanitizeText(exact, TITLE_MAX, "x")).toBe(exact);
    const emoji = sanitizeText("😀".repeat(TITLE_MAX), TITLE_MAX, "x");
    expect(emoji.length).toBeLessThanOrEqual(TITLE_MAX);
    expect(emoji).not.toMatch(/[\ud800-\udbff]…$/);
    expect(sanitizeText("b".repeat(BODY_MAX + 1), BODY_MAX, "")).toHaveLength(BODY_MAX);
  });
});

describe("useNotifications (데스크톱)", () => {
  const run = () =>
    renderHook(() => useNotifications({ enabled: true, pollMs: 30_000, now: () => NOW }));

  it("브라우저 Notification 대신 셸 알림을 쓰고, 같은 알림은 이 화면에서 다시 보내지 않는다", async () => {
    desktop = installDesktop();
    const browser = installNotification("granted");
    stubFetch(() => json(arrivedBody([item({ id: "n1" })])));
    const { result } = run();
    await act(async () => {
      await result.current.refresh();
    });
    await act(async () => {
      await result.current.refresh();
    });
    expect(desktop.notify).toHaveBeenCalledTimes(1);
    expect(browser.instances).toHaveLength(0);
  });

  it("브라우저 권한이 없어도 데스크톱에서는 알림을 보낸다", async () => {
    desktop = installDesktop();
    installNotification("default");
    stubFetch(() => json(arrivedBody([item({ id: "n2" })])));
    const { result } = run();
    await act(async () => {
      await result.current.refresh();
    });
    expect(desktop.notify).toHaveBeenCalledTimes(1);
  });

  it("읽지 않은 수가 바뀌면 배지를 갱신하고, 끝나면 지운다", async () => {
    desktop = installDesktop();
    stubFetch(() =>
      json(arrivedBody([item({ id: "a" }), item({ id: "b" }), item({ id: "c", read: true })])),
    );
    const { result, unmount } = run();
    await act(async () => {
      await result.current.refresh();
    });
    expect(desktop.setBadge).toHaveBeenLastCalledWith(2);
    unmount();
    expect(desktop.setBadge).toHaveBeenLastCalledWith(0);
  });

  it("웹에서는 브라우저 알림을 그대로 쓴다", async () => {
    const browser = installNotification("granted");
    stubFetch(() => json(arrivedBody([item({ id: "w1" })])));
    const { result } = run();
    await act(async () => {
      await result.current.refresh();
    });
    expect(browser.instances).toHaveLength(1);
  });
});

describe("useDesktopBadge", () => {
  it("웹에서는 아무것도 하지 않는다", () => {
    expect(() =>
      renderHook(() => {
        useDesktopBadge(3);
      }),
    ).not.toThrow();
  });

  it("값이 바뀔 때마다 셸에 알린다", () => {
    desktop = installDesktop();
    const { rerender } = renderHook(
      ({ n }) => {
        useDesktopBadge(n);
      },
      { initialProps: { n: 1 } },
    );
    rerender({ n: 4 });
    expect(desktop.setBadge.mock.calls.map(([n]) => n)).toEqual([1, 4]);
  });
});

describe("useDesktopVoiceToggle", () => {
  it("단축키가 오면 toggle을 부르고, 화면이 사라지면 해제한다", () => {
    desktop = installDesktop();
    const toggle = vi.fn();
    const { unmount } = renderHook(() => {
      useDesktopVoiceToggle({ supported: true, toggle });
    });
    desktop.pressShortcut();
    desktop.pressShortcut();
    expect(toggle).toHaveBeenCalledTimes(2);
    unmount();
    expect(desktop.listeners()).toBe(0);
  });

  it("마이크를 지원하지 않으면 아무것도 하지 않는다", () => {
    desktop = installDesktop();
    const toggle = vi.fn();
    renderHook(() => {
      useDesktopVoiceToggle({ supported: false, toggle });
    });
    desktop.pressShortcut();
    expect(toggle).not.toHaveBeenCalled();
  });

  it("웹에서는 아무것도 등록하지 않는다", () => {
    expect(() =>
      renderHook(() => {
        useDesktopVoiceToggle({ supported: true, toggle: vi.fn() });
      }),
    ).not.toThrow();
  });

  it("실제 useVoiceInput과 연결하면 단축키로 녹음이 켜지고 꺼진다", async () => {
    desktop = installDesktop();
    const mic = installFakeMic();
    try {
      const transcribe = vi.fn(() => Promise.resolve("합성 문장"));
      const onTranscript = vi.fn();
      const { result } = renderHook(() => {
        const voice = useVoiceInput({ onTranscript, transcribe });
        useDesktopVoiceToggle(voice);
        return voice;
      });
      await act(async () => {
        desktop?.pressShortcut();
        await Promise.resolve();
      });
      expect(result.current.phase).toBe("recording");
      await act(async () => {
        desktop?.pressShortcut();
        await Promise.resolve();
      });
      expect(onTranscript).toHaveBeenCalledWith("합성 문장");
      expect(mic.track.stop).toHaveBeenCalled();
    } finally {
      mic.restore();
    }
  });

  it("데스크톱에서 마이크가 거부되면 시스템 설정을 안내한다", async () => {
    desktop = installDesktop();
    const mic = installFakeMic({ error: new DOMException("denied", "NotAllowedError") });
    try {
      const { result } = renderHook(() => useVoiceInput({ onTranscript: vi.fn() }));
      await act(async () => {
        await result.current.start();
      });
      expect(result.current.error).toBe(VOICE_TEXT.permissionDesktop);
    } finally {
      mic.restore();
    }
  });
});

describe("알림 패널", () => {
  const api: NotificationsApi = {
    state: { phase: "ready", items: [], unread: 0 },
    refresh: () => Promise.resolve(),
    markRead: () => Promise.resolve(),
    markAllRead: () => Promise.resolve(),
  };

  function renderPanel() {
    stubFetch((url) => {
      if (url === "/api/notifications/upcoming") return json({ notifications: [] });
      return json({ event_reminders_enabled: true, lead_minutes: 10 });
    });
    render(<NotificationPanel notifications={api} onClose={() => undefined} />);
    return screen.getByRole("dialog", { name: "알림" });
  }

  it("데스크톱에서는 브라우저 알림 켜기 대신 데스크톱 안내를 보인다", async () => {
    desktop = installDesktop();
    installNotification("default");
    const dialog = renderPanel();
    expect(
      await within(dialog).findByRole("heading", { name: "데스크톱 알림" }),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "브라우저 알림 켜기" })).toBeNull();
    expect(within(dialog).queryByRole("heading", { name: "브라우저 알림" })).toBeNull();
  });

  it("웹에서는 지금처럼 브라우저 알림 켜기가 보인다", async () => {
    installNotification("default");
    const dialog = renderPanel();
    expect(
      await within(dialog).findByRole("button", { name: "브라우저 알림 켜기" }),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("heading", { name: "데스크톱 알림" })).toBeNull();
  });
});
