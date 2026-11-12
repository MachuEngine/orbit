import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NotificationItem, NotificationSettings } from "../api/schemas";
import css from "./notifications.css?raw";
import { NotificationCenter } from "./NotificationCenter";
import { NOW, arrivedBody, installNotification, item, json } from "./support";
import { useNotifications } from "./useNotifications";

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function stubApi(handler: Handler) {
  const mock = vi
    .fn()
    .mockImplementation((url: string, init?: RequestInit) => Promise.resolve(handler(url, init)));
  vi.stubGlobal("fetch", mock);
  return mock;
}

function bodyOf(init: RequestInit | undefined): string {
  return typeof init?.body === "string" ? init.body : "";
}

function calls(mock: ReturnType<typeof stubApi>) {
  return (mock.mock.calls as [string, RequestInit | undefined][]).map(
    ([url, init]) => `${init?.method ?? "GET"} ${url}`,
  );
}

/** 서버 흉내: 알림·설정을 메모리에 두고 API 경로에 맞게 응답한다. */
function server(initial: {
  arrived?: NotificationItem[];
  upcoming?: NotificationItem[];
  settings?: NotificationSettings;
  cancelStatus?: number;
  putStatus?: number;
}) {
  const state = {
    arrived: initial.arrived ?? [],
    upcoming: initial.upcoming ?? [],
    settings: initial.settings ?? { event_reminders_enabled: true, lead_minutes: 10 },
  };
  const mock = stubApi((url, init) => {
    const method = init?.method ?? "GET";
    if (method === "GET" && url === "/api/notifications") return json(arrivedBody(state.arrived));
    if (method === "GET" && url === "/api/notifications/upcoming")
      return json({ notifications: state.upcoming });
    if (method === "GET" && url === "/api/notification-settings") return json(state.settings);
    if (method === "PUT" && url === "/api/notification-settings") {
      if (initial.putStatus !== undefined) return json({}, initial.putStatus);
      state.settings = JSON.parse(bodyOf(init)) as NotificationSettings;
      return json(state.settings);
    }
    const read = /^\/api\/notifications\/([^/]+)\/read$/.exec(url);
    if (method === "POST" && read !== null) {
      state.arrived = state.arrived.map((n) => (n.id === read[1] ? { ...n, read: true } : n));
      return json({ id: read[1] });
    }
    if (method === "POST" && url === "/api/notifications/read-all") {
      state.arrived = state.arrived.map((n) => ({ ...n, read: true }));
      return json({ updated: 1 });
    }
    const del = /^\/api\/notifications\/([^/]+)$/.exec(url);
    if (method === "DELETE" && del !== null) {
      if (initial.cancelStatus !== undefined) {
        if (initial.cancelStatus === 409) state.upcoming = [];
        return json({}, initial.cancelStatus);
      }
      state.upcoming = state.upcoming.filter((n) => n.id !== del[1]);
      return json({ id: del[1] });
    }
    return json({}, 404);
  });
  return { mock, state };
}

function Harness() {
  const notifications = useNotifications({ enabled: true, pollMs: 1_000_000, now: () => NOW });
  return <NotificationCenter notifications={notifications} now={() => NOW} />;
}

const UPCOMING = item({
  id: "u1",
  kind: "reminder",
  title: "합성 보고서 제출",
  due_at: new Date(NOW + 20 * 60_000).toISOString(),
  event_start: null,
  read: false,
});

beforeEach(() => {
  document.cookie = "orbit_csrf=csrf-test-value";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /^알림 \d+개$/ }));
  return screen.findByRole("dialog", { name: "알림" });
}

describe("알림 버튼", () => {
  it("이름에 읽지 않은 수를 넣고 배지에 같은 수를 보인다", async () => {
    server({ arrived: [item({ id: "a" }), item({ id: "b" }), item({ id: "c", read: true })] });
    render(<Harness />);
    const bell = await screen.findByRole("button", { name: "알림 2개" });
    expect(within(bell).getByText("2")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("읽지 않은 알림 2개");
  });

  it("읽지 않은 알림이 없으면 배지를 보이지 않는다", async () => {
    server({ arrived: [item({ read: true })] });
    render(<Harness />);
    const bell = await screen.findByRole("button", { name: "알림 0개" });
    expect(bell).toHaveTextContent("");
  });

  it("99개를 넘으면 배지를 99+로 줄인다", async () => {
    stubApi((url) =>
      url === "/api/notifications"
        ? json({ notifications: [item()], unread_count: 120 })
        : json({}, 404),
    );
    render(<Harness />);
    const bell = await screen.findByRole("button", { name: "알림 120개" });
    expect(within(bell).getByText("99+")).toBeInTheDocument();
  });
});

describe("패널", () => {
  it("대화 상자로 열리고 도착한 알림·예정된 알림·설정을 보인다", async () => {
    server({ arrived: [item()], upcoming: [UPCOMING] });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveFocus();
    const arrived = within(dialog).getByRole("region", { name: "도착한 알림" });
    expect(within(arrived).getByText("합성 주간 회의")).toBeInTheDocument();
    expect(within(arrived).getByText("읽지 않음")).toBeInTheDocument();
    const upcoming = await within(dialog).findByRole("region", { name: "예정된 알림" });
    expect(await within(upcoming).findByText("합성 보고서 제출")).toBeInTheDocument();
    expect(within(upcoming).getByText("20분 뒤")).toBeInTheDocument();
    expect(await within(dialog).findByRole("checkbox", { name: "일정 알림 켜기" })).toBeChecked();
    expect(within(dialog).getByRole("combobox", { name: "알림 시점" })).toHaveValue("10");
  });

  it("비어 있으면 빈 상태 문구를 보인다", async () => {
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    expect(await within(dialog).findByText("도착한 알림이 없습니다.")).toBeInTheDocument();
    expect(await within(dialog).findByText("예정된 알림이 없습니다.")).toBeInTheDocument();
  });

  it("알림 제목을 HTML로 해석하지 않고 텍스트로만 그린다", async () => {
    const hostile = "<img src=x onerror=alert(1)> [링크](https://evil.example)";
    server({ arrived: [item({ title: hostile })], upcoming: [{ ...UPCOMING, title: hostile }] });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    expect(await within(dialog).findAllByText(hostile)).toHaveLength(2);
    expect(dialog.querySelector("img")).toBeNull();
    expect(dialog.querySelector("a")).toBeNull();
  });

  it("Esc로 닫고 알림 버튼으로 포커스를 돌려준다", async () => {
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    const bell = await screen.findByRole("button", { name: "알림 0개" });
    await open(user);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(bell).toHaveFocus();
  });

  it("닫기 버튼과 배경 누르기로도 닫힌다", async () => {
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    const bell = await screen.findByRole("button", { name: "알림 0개" });
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("button", { name: "닫기" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(bell).toHaveFocus();
    await open(user);
    // 배경은 접근성 트리에서 숨겨져 있어 DOM에서 직접 누른다.
    const backdrop = document.querySelector<HTMLElement>(".notify-backdrop");
    if (backdrop === null) throw new Error("배경이 없다");
    await user.click(backdrop);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Tab 포커스가 패널 밖으로 나가지 않고 처음과 끝을 순환한다", async () => {
    server({ arrived: [item()], upcoming: [UPCOMING] });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await within(dialog).findByRole("checkbox", { name: "일정 알림 켜기" });
    const close = within(dialog).getByRole("button", { name: "닫기" });
    await user.tab({ shift: true }); // 대화 상자 자체에서 거꾸로 가면 마지막 요소다
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(close);
    await user.tab();
    expect(close).toHaveFocus(); // 마지막에서 앞으로 가면 첫 요소다
  });

  it("패널 안에 알림 버튼 같은 바깥 요소로 포커스가 새지 않는다", async () => {
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await within(dialog).findByText("도착한 알림이 없습니다.");
    for (let i = 0; i < 12; i += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });
});

describe("포커스 유지", () => {
  it("알림 상태가 바뀌어 다시 그려도 패널 안의 포커스가 컨테이너로 튀지 않는다", async () => {
    server({ arrived: [item()], upcoming: [UPCOMING] });
    const user = userEvent.setup();
    const ready = (items: NotificationItem[]) => ({
      state: { phase: "ready" as const, items, unread: items.length },
      refresh: () => Promise.resolve(),
      markRead: () => Promise.resolve(),
      markAllRead: () => Promise.resolve(),
    });
    const view = render(<NotificationCenter notifications={ready([item()])} now={() => NOW} />);
    const dialog = await open(user);
    const checkbox = await within(dialog).findByRole("checkbox", { name: "일정 알림 켜기" });
    checkbox.focus();
    view.rerender(
      <NotificationCenter
        notifications={ready([item(), item({ id: "n2", title: "합성 새 알림" })])}
        now={() => NOW}
      />,
    );
    expect(await within(dialog).findByText("합성 새 알림")).toBeInTheDocument();
    expect(checkbox).toHaveFocus();
    expect(dialog).not.toHaveFocus();
  });
});

describe("읽음 처리", () => {
  it("읽음을 누르면 서버에 알리고 읽지 않은 수가 줄어든다", async () => {
    const { mock } = server({ arrived: [item({ id: "a" }), item({ id: "b", title: "합성 B" })] });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("button", { name: "읽음으로 표시: 합성 B" }));
    await screen.findByRole("button", { name: "알림 1개" });
    expect(calls(mock)).toContain("POST /api/notifications/b/read");
    expect(within(dialog).queryByRole("button", { name: "읽음으로 표시: 합성 B" })).toBeNull();
  });

  it("모두 읽음을 누르면 모두 읽은 상태가 되고 버튼이 꺼진다", async () => {
    const { mock } = server({ arrived: [item({ id: "a" }), item({ id: "b" })] });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("button", { name: "모두 읽음" }));
    await screen.findByRole("button", { name: "알림 0개" });
    expect(calls(mock)).toContain("POST /api/notifications/read-all");
    expect(within(dialog).getByRole("button", { name: "모두 읽음" })).toBeDisabled();
  });

  it("읽음 표시에 실패하면 안내한다", async () => {
    stubApi((url, init) => {
      if (url === "/api/notifications" && (init?.method ?? "GET") === "GET")
        return json(arrivedBody([item()]));
      if (url === "/api/notifications/upcoming") return json({ notifications: [] });
      if (url === "/api/notification-settings")
        return json({ event_reminders_enabled: true, lead_minutes: 10 });
      return json({}, 500);
    });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("button", { name: /^읽음으로 표시/ }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "읽음 표시를 하지 못했습니다",
    );
  });
});

describe("예정된 알림 취소", () => {
  it("확인을 거쳐 취소하고 목록에서 사라진다", async () => {
    const { mock } = server({ upcoming: [UPCOMING] });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(
      await within(dialog).findByRole("button", { name: "예정된 알림 취소: 합성 보고서 제출" }),
    );
    const confirm = within(dialog).getByRole("group", { name: /취소할까요/ });
    expect(calls(mock)).not.toContain("DELETE /api/notifications/u1"); // 확인 전에는 보내지 않는다
    await user.click(within(confirm).getByRole("button", { name: "그대로 두기" }));
    expect(within(dialog).queryByRole("group", { name: /취소할까요/ })).toBeNull();

    await user.click(
      within(dialog).getByRole("button", { name: "예정된 알림 취소: 합성 보고서 제출" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "알림 취소" }));
    expect(await within(dialog).findByText("예정된 알림을 취소했습니다.")).toBeInTheDocument();
    expect(calls(mock)).toContain("DELETE /api/notifications/u1");
    expect(within(dialog).queryByText("합성 보고서 제출")).toBeNull();
    expect(within(dialog).getByRole("heading", { name: "예정된 알림" })).toHaveFocus();
  });

  it("이미 도착했거나 취소된 알림이면(409) 목록을 새로 읽고 안내한다", async () => {
    server({ upcoming: [UPCOMING], cancelStatus: 409 });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(await within(dialog).findByRole("button", { name: /^예정된 알림 취소/ }));
    await user.click(within(dialog).getByRole("button", { name: "알림 취소" }));
    expect(await within(dialog).findByText(/이미 도착했거나 취소된 알림/)).toBeInTheDocument();
    expect(within(dialog).queryByText("합성 보고서 제출")).toBeNull();
  });

  it("서버 오류면 취소하지 못했다고 알린다", async () => {
    server({ upcoming: [UPCOMING], cancelStatus: 500 });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(await within(dialog).findByRole("button", { name: /^예정된 알림 취소/ }));
    await user.click(within(dialog).getByRole("button", { name: "알림 취소" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "알림을 취소하지 못했습니다",
    );
    expect(within(dialog).getByText("합성 보고서 제출")).toBeInTheDocument();
  });
});

describe("일정 알림 설정", () => {
  it("켜기·끄기를 바꾸면 전체 값을 저장한다", async () => {
    const { mock } = server({ settings: { event_reminders_enabled: true, lead_minutes: 30 } });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(await within(dialog).findByRole("checkbox", { name: "일정 알림 켜기" }));
    expect(await within(dialog).findByText("설정을 저장했습니다.")).toBeInTheDocument();
    const put = (mock.mock.calls as [string, RequestInit | undefined][]).find(
      ([, init]) => init?.method === "PUT",
    );
    expect(JSON.parse(bodyOf(put?.[1]))).toEqual({
      event_reminders_enabled: false,
      lead_minutes: 30,
    });
    expect(within(dialog).getByRole("checkbox", { name: "일정 알림 켜기" })).not.toBeChecked();
    expect(within(dialog).getByRole("combobox", { name: "알림 시점" })).toBeDisabled();
  });

  it("알림 시점은 다섯 가지 중에서 고르고 저장 뒤 예정 목록을 다시 읽는다", async () => {
    const { mock } = server({ upcoming: [UPCOMING] });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    const select = await within(dialog).findByRole("combobox", { name: "알림 시점" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual([
      "일정 시작 5분 전",
      "일정 시작 10분 전",
      "일정 시작 15분 전",
      "일정 시작 30분 전",
      "일정 시작 60분 전",
    ]);
    const before = calls(mock).filter((c) => c === "GET /api/notifications/upcoming").length;
    await user.selectOptions(select, "30");
    await within(dialog).findByText("설정을 저장했습니다.");
    const put = (mock.mock.calls as [string, RequestInit | undefined][]).find(
      ([, init]) => init?.method === "PUT",
    );
    expect(JSON.parse(bodyOf(put?.[1]))).toEqual({
      event_reminders_enabled: true,
      lead_minutes: 30,
    });
    expect(calls(mock).filter((c) => c === "GET /api/notifications/upcoming")).toHaveLength(
      before + 1,
    );
  });

  it("저장에 실패하면 안내하고 이전 값을 유지한다", async () => {
    server({ putStatus: 500 });
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(await within(dialog).findByRole("checkbox", { name: "일정 알림 켜기" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "설정을 저장하지 못했습니다",
    );
    expect(within(dialog).getByRole("checkbox", { name: "일정 알림 켜기" })).toBeChecked();
  });
});

describe("브라우저 알림 켜기", () => {
  it("권한이 default일 때만 버튼을 보이고, 누르면 그때 권한을 묻는다", async () => {
    const fake = installNotification("default");
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("button", { name: "알림 0개" });
    expect(fake.requestPermission).not.toHaveBeenCalled(); // 화면을 열기만 해서는 묻지 않는다
    const dialog = await open(user);
    expect(fake.requestPermission).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "브라우저 알림 켜기" }));
    expect(fake.requestPermission).toHaveBeenCalledTimes(1);
    expect(await within(dialog).findByText(/브라우저 알림이 켜져 있습니다/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "브라우저 알림 켜기" })).toBeNull();
  });

  it("거부돼 있으면 버튼 없이 허용 방법을 안내한다", async () => {
    installNotification("denied");
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    expect(within(dialog).queryByRole("button", { name: "브라우저 알림 켜기" })).toBeNull();
    expect(within(dialog).getByRole("note")).toHaveTextContent("사이트 설정에서 알림을 허용");
  });

  it("이미 허용돼 있으면 켜져 있다고만 알린다", async () => {
    installNotification("granted");
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    expect(within(dialog).queryByRole("button", { name: "브라우저 알림 켜기" })).toBeNull();
    expect(within(dialog).getByText(/브라우저 알림이 켜져 있습니다/)).toBeInTheDocument();
  });

  it("권한 요청 결과가 거부면 안내로 바뀐다", async () => {
    const fake = installNotification("default");
    fake.requestPermission.mockImplementation(() => {
      fake.permission = "denied";
      return Promise.resolve("denied" as const);
    });
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("button", { name: "브라우저 알림 켜기" }));
    await waitFor(() => {
      expect(within(dialog).getByRole("note")).toBeInTheDocument();
    });
  });

  it("Notification을 지원하지 않으면 이 구역을 보이지 않는다", async () => {
    vi.stubGlobal("Notification", undefined);
    server({});
    const user = userEvent.setup();
    render(<Harness />);
    const dialog = await open(user);
    expect(within(dialog).queryByRole("heading", { name: "브라우저 알림" })).toBeNull();
  });
});

describe("스타일 규칙", () => {
  it("색·글자 크기를 토큰으로만 쓰고 패널이 좁은 화면 너비를 넘지 않는다", () => {
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(css).not.toMatch(/font-size:\s*[\d.]+(px|rem|em)/);
    expect(css).toMatch(/\.notify-panel\s*{[^}]*width:\s*min\(26rem,\s*100vw\)/);
  });
});
