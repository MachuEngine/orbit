import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NOTIFICATION_POLL_MS, useNotifications } from "./useNotifications";
import {
  NOW,
  arrivedBody,
  installNotification,
  item,
  json,
  resetHidden,
  setHidden,
} from "./support";

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function stubApi(handler: Handler) {
  const mock = vi
    .fn()
    .mockImplementation((url: string, init?: RequestInit) => Promise.resolve(handler(url, init)));
  vi.stubGlobal("fetch", mock);
  return mock;
}

const listCalls = (mock: ReturnType<typeof stubApi>) =>
  (mock.mock.calls as [string][]).filter(([url]) => url === "/api/notifications").length;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
  document.cookie = "orbit_csrf=csrf-test-value";
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetHidden();
});

const hook = (options: Partial<Parameters<typeof useNotifications>[0]> = {}) =>
  renderHook(() => useNotifications({ enabled: true, now: () => NOW, ...options }));

describe("폴링", () => {
  it("켜지면 바로 한 번 읽고 30초마다 다시 읽는다", async () => {
    const mock = stubApi(() => json(arrivedBody([item()])));
    const { result } = hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(listCalls(mock)).toBe(1);
    expect(result.current.state).toMatchObject({ phase: "ready", unread: 1 });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS);
    });
    expect(listCalls(mock)).toBe(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 2);
    });
    expect(listCalls(mock)).toBe(4);
  });

  it("enabled가 꺼져 있으면 읽지 않는다", async () => {
    const mock = stubApi(() => json(arrivedBody([])));
    hook({ enabled: false });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 3);
    });
    expect(listCalls(mock)).toBe(0);
  });

  it("탭이 숨겨지면 멈추고 다시 보이면 바로 읽은 뒤 이어간다", async () => {
    const mock = stubApi(() => json(arrivedBody([])));
    hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(listCalls(mock)).toBe(1);

    await act(async () => {
      setHidden(true);
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 4);
    });
    expect(listCalls(mock)).toBe(1); // 숨겨진 동안은 읽지 않는다

    await act(async () => {
      setHidden(false);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(listCalls(mock)).toBe(2); // 보이면 바로 읽는다
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS);
    });
    expect(listCalls(mock)).toBe(3); // 그 뒤 30초 간격이 이어진다
  });

  it("숨겨진 채로 시작하면 보일 때까지 읽지 않는다", async () => {
    const mock = stubApi(() => json(arrivedBody([])));
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 2);
    });
    expect(listCalls(mock)).toBe(0);
    await act(async () => {
      setHidden(false);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(listCalls(mock)).toBe(1);
  });

  it("화면을 떠나면 타이머와 이벤트 처리기를 거둔다", async () => {
    const mock = stubApi(() => json(arrivedBody([])));
    const { unmount } = hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    unmount();
    await act(async () => {
      setHidden(false);
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 3);
    });
    expect(listCalls(mock)).toBe(1);
  });

  it("로그인이 끝나면(401) 폴링을 멈추고 알린다", async () => {
    const mock = stubApi(() => json({ detail: "unauthorized" }, 401));
    const onUnauthorized = vi.fn();
    hook({ onUnauthorized });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 3);
    });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(listCalls(mock)).toBe(1);
  });

  it("한 번 실패해도 읽어 둔 목록은 남고 다음 주기에 다시 읽는다", async () => {
    let fail = false;
    stubApi(() => (fail ? json({}, 500) : json(arrivedBody([item()]))));
    const { result } = hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    fail = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS);
    });
    expect(result.current.state).toMatchObject({ phase: "ready", unread: 1 });
  });

  it("처음부터 실패하면 오류 상태다", async () => {
    stubApi(() => json({}, 500));
    const { result } = hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state).toEqual({ phase: "error" });
  });
});

describe("브라우저 알림", () => {
  const due = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString();

  it("새로 도착한 읽지 않은 알림만 한 번 띄운다(중복 방지)", async () => {
    const fake = installNotification("granted");
    const fresh = item({ id: "fresh", title: "합성 새 알림", due_at: due(1) });
    const old = item({ id: "old", due_at: due(30) });
    const read = item({ id: "read", due_at: due(2), read: true });
    stubApi(() => json(arrivedBody([fresh, old, read])));
    hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fake.instances.map((n) => [n.title, n.options?.tag])).toEqual([
      ["합성 새 알림", "fresh"],
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 3); // 같은 알림은 다시 띄우지 않는다
    });
    expect(fake.instances).toHaveLength(1);
  });

  it("폴링 중 새로 도착한 알림은 그때 띄운다", async () => {
    const fake = installNotification("granted");
    let items = [item({ id: "a", due_at: due(1) })];
    stubApi(() => json(arrivedBody(items)));
    hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    items = [item({ id: "b", title: "합성 두 번째", due_at: due(0) }), ...items];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS);
    });
    expect(fake.instances.map((n) => n.options?.tag)).toEqual(["a", "b"]);
  });

  it("권한이 허용되지 않았으면 띄우지 않고, 허용된 뒤 아직 10분 안이면 띄운다", async () => {
    const fake = installNotification("default");
    stubApi(() => json(arrivedBody([item({ id: "a", due_at: due(1) })])));
    hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fake.instances).toEqual([]);
    fake.permission = "granted";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS);
    });
    expect(fake.instances.map((n) => n.options?.tag)).toEqual(["a"]);
  });

  it("Notification을 지원하지 않는 브라우저에서도 목록은 읽는다", async () => {
    vi.stubGlobal("Notification", undefined);
    stubApi(() => json(arrivedBody([item({ due_at: due(1) })])));
    const { result } = hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state).toMatchObject({ phase: "ready" });
  });
});

describe("읽음 처리", () => {
  it("읽음·전체 읽음을 보낸 뒤 목록을 다시 읽는다", async () => {
    let unread = true;
    const mock = stubApi((url, init) => {
      if (url === "/api/notifications") return json(arrivedBody([item({ read: !unread })]));
      if (url === "/api/notifications/n1/read" && init?.method === "POST") {
        unread = false;
        return json({ id: "n1" });
      }
      if (url === "/api/notifications/read-all") return json({ updated: 1 });
      return json({}, 404);
    });
    const { result } = hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await result.current.markRead("n1");
    });
    expect(result.current.state).toMatchObject({ phase: "ready", unread: 0 });
    const posts = (mock.mock.calls as [string, RequestInit | undefined][]).filter(
      ([, init]) => init?.method === "POST",
    );
    expect(posts.map(([url]) => url)).toEqual(["/api/notifications/n1/read"]);
    expect((posts[0]?.[1]?.headers as Record<string, string> | undefined)?.["X-CSRF-Token"]).toBe(
      "csrf-test-value",
    );
    await act(async () => {
      await result.current.markAllRead();
    });
    expect(mock.mock.calls.some(([url]) => url === "/api/notifications/read-all")).toBe(true);
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("응답이 늦게 도착할 때", () => {
  it("응답이 오기 전에 탭이 숨겨지면 응답이 와도 폴링을 다시 시작하지 않는다", async () => {
    const gate = deferred<Response>();
    let first = true;
    const mock = vi.fn().mockImplementation(() => {
      if (first) {
        first = false;
        return gate.promise;
      }
      return Promise.resolve(json(arrivedBody([])));
    });
    vi.stubGlobal("fetch", mock);
    hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      setHidden(true);
      gate.resolve(json(arrivedBody([])));
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 4);
    });
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it("응답이 오기 전에 화면을 떠나면 응답이 와도 타이머를 만들지 않는다", async () => {
    const gate = deferred<Response>();
    const mock = vi.fn().mockImplementation(() => gate.promise);
    vi.stubGlobal("fetch", mock);
    const { unmount } = hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    unmount();
    await act(async () => {
      gate.resolve(json(arrivedBody([])));
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS * 4);
    });
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it("진행 중이던 폴링의 오래된 응답이 읽음 처리 결과를 되돌리지 않는다", async () => {
    const gates: ReturnType<typeof deferred<Response>>[] = [];
    let listCount = 0;
    const mock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url === "/api/notifications" && (init?.method ?? "GET") === "GET") {
        listCount += 1;
        if (listCount === 1) return Promise.resolve(json(arrivedBody([item()])));
        const gate = deferred<Response>();
        gates.push(gate);
        return gate.promise;
      }
      return Promise.resolve(json({ id: "n1" }));
    });
    vi.stubGlobal("fetch", mock);
    const { result } = hook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state).toMatchObject({ unread: 1 });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS); // 폴링 요청이 서버 응답 전이다
    });
    expect(gates).toHaveLength(1);
    let marking!: Promise<void>;
    await act(async () => {
      marking = result.current.markRead("n1"); // 읽음 뒤 읽기는 진행 중 폴링에 막히지 않는다
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(gates).toHaveLength(2);
    await act(async () => {
      gates[1]?.resolve(json(arrivedBody([item({ read: true })])));
      await marking;
    });
    expect(result.current.state).toMatchObject({ unread: 0 });
    await act(async () => {
      gates[0]?.resolve(json(arrivedBody([item()]))); // 읽음 이전에 만든 응답이 뒤늦게 온다
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state).toMatchObject({ unread: 0 }); // 오래된 응답은 버린다
  });
});
