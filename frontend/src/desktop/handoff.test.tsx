import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsScreen } from "../connections/ConnectionsScreen";
import { LoginScreen } from "../session/LoginScreen";
import { useSession } from "../session/useSession";
import { getConnectBridge, getLoginBridge } from "./bridge";
import { installDesktop, type FakeDesktop } from "./support";

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(handler: Handler) {
  const mock = vi
    .fn()
    .mockImplementation((url: string, init?: RequestInit) => Promise.resolve(handler(url, init)));
  vi.stubGlobal("fetch", mock);
  return mock;
}

function calls(mock: ReturnType<typeof stubApi>): [string, RequestInit | undefined][] {
  return mock.mock.calls as [string, RequestInit | undefined][];
}

let desktop: FakeDesktop | undefined;

beforeEach(() => {
  document.cookie = "orbit_csrf=csrf-test-value";
});

afterEach(() => {
  desktop?.uninstall();
  desktop = undefined;
  vi.unstubAllGlobals();
  document.cookie = "orbit_csrf=; max-age=0";
});

const METHODS = { dev: false, google: true };

function login() {
  return render(
    <LoginScreen
      methods={METHODS}
      devLoginFailed={false}
      loginError={null}
      onDevSignIn={() => Promise.resolve()}
    />,
  );
}

describe("브리지 확인", () => {
  it("웹에서는 로그인·연결 브리지가 없다", () => {
    expect(getLoginBridge()).toBeUndefined();
    expect(getConnectBridge()).toBeUndefined();
  });

  it("핸드오프 함수가 없는 옛 앱은 웹 방식으로 돌아간다", () => {
    desktop = installDesktop();
    const api = window.orbitDesktop;
    expect(api).toBeDefined();
    const old = { ...desktop.api };
    delete old.startLogin;
    delete old.openConnectFlow;
    delete old.prepareConnect;
    window.orbitDesktop = old;
    expect(getLoginBridge()).toBeUndefined();
    expect(getConnectBridge()).toBeUndefined();
  });
});

describe("로그인 화면", () => {
  it("웹에서는 기존처럼 /api/auth/google/start 링크다", () => {
    login();
    const link = screen.getByRole("link", { name: "Google로 로그인" });
    expect(link).toHaveAttribute("href", "/api/auth/google/start");
  });

  it("데스크톱에서는 링크 대신 버튼이고, 누르면 startLogin을 부르고 대기 안내를 보인다", async () => {
    desktop = installDesktop();
    login();
    expect(screen.queryByRole("link", { name: "Google로 로그인" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Google로 로그인" }));
    expect(desktop.startLogin).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/브라우저에서 로그인을 마치세요/)).toBeInTheDocument();
    // 대기 중에도 다시 눌러 브라우저를 다시 열 수 있다.
    await userEvent.click(screen.getByRole("button", { name: "브라우저 다시 열기" }));
    expect(desktop.startLogin).toHaveBeenCalledTimes(2);
  });

  it("실패 이벤트가 오면 대기를 풀고 오류 문구를 보인다", async () => {
    desktop = installDesktop();
    login();
    await userEvent.click(screen.getByRole("button", { name: "Google로 로그인" }));
    act(() => {
      desktop?.emitLogin({ status: "failed", reason: "not_allowed" });
    });
    expect(await screen.findByText(/이 Google 계정은 아직 사용할 수 없습니다/)).toBeInTheDocument();
    expect(screen.queryByText(/브라우저에서 로그인을 마치세요/)).toBeNull();
    expect(screen.getByRole("button", { name: "Google로 로그인" })).toBeInTheDocument();
  });

  it.each([
    ["expired", /로그인 시간이 지났습니다/],
    ["state", /로그인 정보를 확인하지 못했습니다/],
    ["session", /이 앱에 저장하지 못했습니다/],
    ["something_else", /로그인하지 못했습니다/],
  ])("실패 종류 %s를 알맞은 문구로 바꾼다", async (reason, pattern) => {
    desktop = installDesktop();
    login();
    await userEvent.click(screen.getByRole("button", { name: "Google로 로그인" }));
    act(() => {
      desktop?.emitLogin({ status: "failed", reason });
    });
    expect(await screen.findByText(pattern)).toBeInTheDocument();
  });

  it("화면을 떠나면 이벤트 구독을 해제한다", () => {
    desktop = installDesktop();
    const view = login();
    view.unmount();
    // 해제 뒤에 이벤트가 와도 오류 없이 무시된다.
    expect(() => {
      desktop?.emitLogin({ status: "failed", reason: "expired" });
    }).not.toThrow();
  });
});

describe("로그아웃", () => {
  const signedIn = {
    authenticated: true,
    workspace_id: "w1",
    expires_at: "2026-11-12T00:00:00+00:00",
    display_name: null,
    email: null,
    dev_login: false,
    google_login: true,
  };

  it("데스크톱이면 로그아웃 API 다음에 clearSession을 부른다", async () => {
    desktop = installDesktop();
    const order: string[] = [];
    stubApi((url, init) => {
      if (url === "/api/logout") {
        order.push("logout");
        return new Response(null, { status: 204 });
      }
      expect(init?.method ?? "GET").toBe("GET");
      return json(signedIn);
    });
    desktop.clearSession.mockImplementation(() => {
      order.push("clearSession");
    });
    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.state.phase).toBe("signed_in");
    });
    await act(async () => {
      await result.current.signOut();
    });
    expect(order).toEqual(["logout", "clearSession"]);
    expect(result.current.state.phase).toBe("signed_out");
  });

  it("로그아웃 API가 실패해도 앱 저장소는 지운다", async () => {
    desktop = installDesktop();
    stubApi((url) => (url === "/api/logout" ? json({ detail: "x" }, 500) : json(signedIn)));
    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.state.phase).toBe("signed_in");
    });
    await act(async () => {
      await result.current.signOut().catch(() => undefined);
    });
    expect(desktop.clearSession).toHaveBeenCalledTimes(1);
  });

  it("웹에서는 clearSession 없이 기존대로 동작한다", async () => {
    stubApi((url) =>
      url === "/api/logout" ? new Response(null, { status: 204 }) : json(signedIn),
    );
    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.state.phase).toBe("signed_in");
    });
    await act(async () => {
      await result.current.signOut();
    });
    expect(result.current.state.phase).toBe("signed_out");
  });
});

const SCOPES = [
  "openid",
  "https://www.googleapis.com/auth/calendar.events",
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.compose",
];
const TICKET_URL =
  "http://localhost:5173/api/connections/google/desktop-begin?ticket=" + "T".repeat(43);

const connection = (extra: Record<string, unknown> = {}) => ({
  id: "c1",
  provider: "google",
  status: "active",
  account_email: "alice@example.test",
  scopes: SCOPES,
  docs_granted: false,
  created_at: "2026-11-12T00:00:00Z",
  ...extra,
});

const listing = (connections: unknown[]) => ({
  mode: "google",
  connect_available: true,
  connections,
});

describe("계정 연결 (데스크톱)", () => {
  const PREPARED = {
    code_challenge: "A".repeat(43),
    port: 53124,
    app_state: "S".repeat(43),
  };

  function ticketApi(listingBody: () => unknown, ticketStatus = 200) {
    return stubApi((url) =>
      url === "/api/connections/google/desktop-ticket"
        ? json(ticketStatus === 200 ? { url: TICKET_URL } : { detail: "x" }, ticketStatus)
        : json(listingBody()),
    );
  }
  const ticketCall = (mock: ReturnType<typeof stubApi>) =>
    calls(mock).find(([url]) => url === "/api/connections/google/desktop-ticket");

  it("연결은 셸이 준비한 값을 티켓 요청에 묶고 openConnectFlow로 연다", async () => {
    desktop = installDesktop();
    const mock = ticketApi(() => listing([]));
    const navigate = vi.fn();
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={navigate} />);
    await userEvent.click(await screen.findByRole("button", { name: "Google 계정 연결" }));
    await waitFor(() => {
      expect(desktop?.openConnectFlow).toHaveBeenCalledWith(TICKET_URL);
    });
    expect(desktop.prepareConnect).toHaveBeenCalledWith({ intent: "connect" });
    expect(navigate).not.toHaveBeenCalled();
    const ticket = ticketCall(mock);
    expect(ticket?.[1]?.method).toBe("POST");
    expect(JSON.parse(ticket?.[1]?.body as string)).toEqual({ intent: "connect", ...PREPARED });
    expect((ticket?.[1]?.headers as Record<string, string>)["X-CSRF-Token"]).toBe(
      "csrf-test-value",
    );
    // code_verifier는 renderer에 없다. 웹 방식 시작 API도 부르지 않는다.
    expect(ticket?.[1]?.body as string).not.toContain("verifier");
    expect(calls(mock).some(([url]) => url === "/api/connections/google/start")).toBe(false);
    expect(await screen.findByText(/브라우저에서 동의를 마친 뒤/)).toBeInTheDocument();
  });

  it("셸이 완료 이벤트를 보내면 목록을 다시 불러오고 결과 문구를 보인다", async () => {
    desktop = installDesktop();
    let connected = false;
    const mock = ticketApi(() => listing(connected ? [connection()] : []));
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Google 계정 연결" }));
    await screen.findByText(/브라우저에서 동의를 마친 뒤/);
    const before = calls(mock).filter(([url]) => url === "/api/connections").length;

    connected = true;
    act(() => {
      desktop?.emitConnect({ status: "done", result: "connected" });
    });
    expect(await screen.findByText("Google 계정을 연결했습니다.")).toBeInTheDocument();
    expect(screen.queryByText(/브라우저에서 동의를 마친 뒤/)).toBeNull();
    await waitFor(() => {
      expect(calls(mock).filter(([url]) => url === "/api/connections").length).toBeGreaterThan(
        before,
      );
    });
  });

  it("문서 권한 추가는 docs_scope 티켓을 받아 열고 완료 문구를 보인다", async () => {
    desktop = installDesktop();
    const mock = ticketApi(() => listing([connection()]));
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "문서 권한 추가" }));
    await waitFor(() => {
      expect(desktop?.openConnectFlow).toHaveBeenCalledWith(TICKET_URL);
    });
    expect(desktop.prepareConnect).toHaveBeenCalledWith({
      intent: "docs_scope",
      connection_id: "c1",
    });
    expect(JSON.parse(ticketCall(mock)?.[1]?.body as string)).toEqual({
      intent: "docs_scope",
      connection_id: "c1",
      ...PREPARED,
    });
    expect(calls(mock).some(([url]) => url.endsWith("/scopes/docs/start"))).toBe(false);
    act(() => {
      desktop?.emitConnect({ status: "done", result: "docs_added" });
    });
    expect(await screen.findByText(/문서 권한을 추가했습니다/)).toBeInTheDocument();
  });

  it.each([
    ["account_mismatch", /다른 Google 계정을 골랐습니다/],
    ["denied", /Google 연결을 취소했습니다/],
    ["expired", /연결 시간이 지났습니다/],
    ["weird_kind", /연결하지 못했습니다/],
  ])("실패 이벤트 %s를 오류 문구로 보인다", async (reason, pattern) => {
    desktop = installDesktop();
    ticketApi(() => listing([]));
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Google 계정 연결" }));
    await screen.findByText(/브라우저에서 동의를 마친 뒤/);
    act(() => {
      desktop?.emitConnect({ status: "failed", reason });
    });
    expect(await screen.findByText(pattern)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Google 계정 연결" })).toBeEnabled();
  });

  it("티켓 발급이 실패하면 시작하지 못했다고 알리고 브라우저를 열지 않는다", async () => {
    desktop = installDesktop();
    ticketApi(() => listing([]), 503);
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Google 계정 연결" }));
    expect(await screen.findByText(/연결을 시작하지 못했습니다/)).toBeInTheDocument();
    expect(desktop.openConnectFlow).not.toHaveBeenCalled();
  });

  it("셸 준비가 실패하면 티켓을 요청하지 않는다", async () => {
    desktop = installDesktop();
    desktop.prepareConnect.mockRejectedValue(new Error("EADDRINUSE"));
    const mock = ticketApi(() => listing([]));
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Google 계정 연결" }));
    expect(await screen.findByText(/연결을 시작하지 못했습니다/)).toBeInTheDocument();
    expect(ticketCall(mock)).toBeUndefined();
  });

  it("화면을 떠나면 이벤트 구독을 해제한다", async () => {
    desktop = installDesktop();
    ticketApi(() => listing([]));
    const view = render(
      <ConnectionsScreen result={null} onBack={() => undefined} navigate={vi.fn()} />,
    );
    await screen.findByText(/연결된 계정이 없습니다/);
    view.unmount();
    expect(() => {
      desktop?.emitConnect({ status: "done", result: "connected" });
    }).not.toThrow();
  });

  it("웹에서는 브리지 없이 기존 시작 API로 이동한다", async () => {
    const mock = stubApi((url) =>
      url === "/api/connections/google/start"
        ? json({ authorization_url: "https://accounts.example.test/auth" })
        : json(listing([])),
    );
    const navigate = vi.fn();
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={navigate} />);
    await userEvent.click(await screen.findByRole("button", { name: "Google 계정 연결" }));
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("https://accounts.example.test/auth");
    });
    expect(ticketCall(mock)).toBeUndefined();
  });
});
