import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ConnectionsScreen } from "./ConnectionsScreen";
import { readConnectionResult, scopeLabels } from "./text";

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(handler: Handler) {
  const fetchMock = vi
    .fn()
    .mockImplementation((url: string, init?: RequestInit) => Promise.resolve(handler(url, init)));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function calls(mock: ReturnType<typeof stubApi>): [string, RequestInit | undefined][] {
  return mock.mock.calls as [string, RequestInit | undefined][];
}

const SCOPES = [
  "openid",
  "https://www.googleapis.com/auth/calendar.events",
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.compose",
];

const activeConnection = {
  id: "c1",
  provider: "google",
  status: "active",
  account_email: "alice@example.test",
  scopes: SCOPES,
  created_at: "2026-11-12T00:00:00Z",
};

function listing(connections: unknown[], extra: Record<string, unknown> = {}) {
  return { mode: "google", connect_available: true, connections, ...extra };
}

beforeEach(() => {
  document.cookie = "orbit_csrf=csrf-test-value";
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
  document.cookie = "orbit_csrf=; max-age=0";
});

describe("연결 화면", () => {
  it("연결이 없으면 안내와 연결 버튼을 보이고, 누르면 받은 주소로 이동한다", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    stubApi((url, init) => {
      calls.push({ url, ...(init === undefined ? {} : { init }) });
      if (url === "/api/connections/google/start") {
        return json({ authorization_url: "https://accounts.example.test/auth?x=1" });
      }
      return json(listing([]));
    });
    const navigate = vi.fn();
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={navigate} />);
    expect(await screen.findByText(/연결된 계정이 없습니다/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Google 계정 연결" }));
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("https://accounts.example.test/auth?x=1");
    });
    const start = calls.find((c) => c.url === "/api/connections/google/start");
    expect(start?.init?.method).toBe("POST");
    expect((start?.init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe(
      "csrf-test-value",
    );
  });

  it("연결 시작에 실패하면 이유와 다음 행동을 알리고 다시 누를 수 있다", async () => {
    stubApi((url) => (url === "/api/connections/google/start" ? json({}, 503) : json(listing([]))));
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Google 계정 연결" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("연결을 시작하지 못했습니다");
    expect(screen.getByRole("button", { name: "Google 계정 연결" })).toBeEnabled();
  });

  it("서버에 연결 설정이 없으면 버튼을 막고 이유를 알린다", async () => {
    stubApi(() => json(listing([], { connect_available: false })));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByRole("button", { name: "Google 계정 연결" })).toBeDisabled();
    expect(screen.getByText(/연결 설정이 없어/)).toBeInTheDocument();
  });

  it("연결된 계정은 이메일과 허용 범위를 사람이 읽는 말로 보인다", async () => {
    stubApi(() => json(listing([activeConnection])));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByText("alice@example.test")).toBeInTheDocument();
    expect(screen.getByText("연결됨")).toBeInTheDocument();
    expect(screen.getByText("일정 보기·만들기·수정")).toBeInTheDocument();
    expect(screen.getByText("메일 읽기·검색")).toBeInTheDocument();
    expect(screen.getByText("메일 초안 작성·보내기")).toBeInTheDocument();
    expect(screen.queryByText(/googleapis\.com/)).toBeNull();
    expect(screen.queryByText("openid")).toBeNull();
  });

  it("해제는 확인 단계를 거치고, 취소하면 요청을 보내지 않는다", async () => {
    const fetchMock = stubApi(() => json(listing([activeConnection])));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: "연결 해제" }));
    expect(screen.getByRole("group", { name: "연결 해제 확인" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "취소" }));
    expect(screen.queryByRole("group", { name: "연결 해제 확인" })).toBeNull();
    const deletes = calls(fetchMock).filter(([, init]) => init?.method === "DELETE");
    expect(deletes).toHaveLength(0);
  });

  it("해제를 확인하면 DELETE를 보내고 목록을 다시 읽어 결과를 알린다", async () => {
    let revoked = false;
    const fetchMock = stubApi((url, init) => {
      if (url === "/api/connections/c1" && init?.method === "DELETE") {
        revoked = true;
        return json({ id: "c1", status: "revoked", provider_revoke: "revoked" });
      }
      return json(listing(revoked ? [] : [activeConnection]));
    });
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: "연결 해제" }));
    const group = screen.getByRole("group", { name: "연결 해제 확인" });
    await userEvent.click(within(group).getByRole("button", { name: "연결 해제" }));
    expect(await screen.findByText(/연결을 해제했습니다/)).toBeInTheDocument();
    expect(await screen.findByText(/연결된 계정이 없습니다/)).toBeInTheDocument();
    const del = calls(fetchMock).find(([, init]) => init?.method === "DELETE");
    expect((del?.[1]?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-test-value");
  });

  it("Google 철회에 실패해도 로컬 해제는 알리고 직접 삭제하는 방법을 안내한다", async () => {
    let revoked = false;
    stubApi((url, init) => {
      if (init?.method === "DELETE") {
        revoked = true;
        return json({ id: "c1", status: "revoked", provider_revoke: "failed" });
      }
      return json(listing(revoked ? [] : [activeConnection]));
    });
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: "연결 해제" }));
    const group = screen.getByRole("group", { name: "연결 해제 확인" });
    await userEvent.click(within(group).getByRole("button", { name: "연결 해제" }));
    expect(
      await screen.findByText(/보안 설정에서 Orbit의 접근 권한을 직접 삭제/),
    ).toBeInTheDocument();
  });

  it("오류 상태의 연결은 다시 연결 버튼을 보인다", async () => {
    stubApi(() => json(listing([{ ...activeConnection, status: "error" }])));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByText(/연결에 문제가 있습니다/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Google 계정 다시 연결" })).toBeInTheDocument();
    expect(screen.queryByText("일정 보기·만들기·수정")).toBeNull();
  });

  it("연결 결과가 오류면 알려진 문구를, 모르는 값은 일반 문구를 보인다", async () => {
    stubApi(() => json(listing([])));
    const { unmount } = render(
      <ConnectionsScreen
        result={{ kind: "error", error: "scope_missing" }}
        onBack={() => undefined}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("필요한 권한이 모두 허용되지 않아");
    unmount();
    render(
      <ConnectionsScreen result={{ kind: "error", error: "<b>x</b>" }} onBack={() => undefined} />,
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("연결하지 못했습니다");
    expect(alert).not.toHaveTextContent("<b>");
  });

  it("목록을 읽지 못하면 알린다", async () => {
    stubApi(() => json({}, 500));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByText(/연결 정보를 불러오지 못했습니다/)).toBeInTheDocument();
  });
});

describe("주소의 연결 결과와 범위 문구", () => {
  it("연결 결과를 읽는다", () => {
    expect(readConnectionResult("?connection=connected")).toEqual({ kind: "connected" });
    expect(readConnectionResult("?connection_error=state")).toEqual({
      kind: "error",
      error: "state",
    });
    expect(readConnectionResult("?connection=other")).toBeNull();
    expect(readConnectionResult("")).toBeNull();
  });

  it("모르는 범위는 보이지 않는다", () => {
    expect(scopeLabels(["x", "https://www.googleapis.com/auth/gmail.readonly"])).toEqual([
      "메일 읽기·검색",
    ]);
  });
});

const signedIn = {
  authenticated: true,
  workspace_id: "w1",
  expires_at: "2026-11-13T00:00:00Z",
  display_name: "앨리스 테스트",
  email: "alice@example.test",
  dev_login: true,
  google_login: true,
};

describe("채팅 화면 안내와 이동", () => {
  it("google 모드에서 연결이 없으면 안내를 보이고, 누르면 연결 화면으로 간다", async () => {
    stubApi((url) => (url === "/api/session" ? json(signedIn) : json(listing([]))));
    render(<App />);
    expect(await screen.findByText(/Google 계정이 연결되지 않아/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "계정 연결하기" }));
    expect(await screen.findByRole("heading", { name: "계정 연결" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "채팅으로 돌아가기" }));
    expect(await screen.findByLabelText("메시지")).toBeInTheDocument();
  });

  it("연결이 있거나 sim 모드면 안내를 보이지 않는다", async () => {
    stubApi((url) => (url === "/api/session" ? json(signedIn) : json(listing([activeConnection]))));
    const { unmount } = render(<App />);
    expect(await screen.findByLabelText("메시지")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText(/Google 계정이 연결되지 않아/)).toBeNull();
    });
    unmount();
    stubApi((url) =>
      url === "/api/session" ? json(signedIn) : json(listing([], { mode: "sim" })),
    );
    render(<App />);
    expect(await screen.findByLabelText("메시지")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText(/Google 계정이 연결되지 않아/)).toBeNull();
    });
  });

  it("연결 결과가 주소에 있으면 연결 화면을 열고 주소에서 지운다", async () => {
    window.history.replaceState(null, "", "/?connection=connected");
    stubApi((url) => (url === "/api/session" ? json(signedIn) : json(listing([activeConnection]))));
    render(<App />);
    expect(await screen.findByText("Google 계정을 연결했습니다.")).toBeInTheDocument();
    expect(window.location.search).toBe("");
  });
});
