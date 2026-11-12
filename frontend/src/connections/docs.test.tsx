import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ConnectionsScreen } from "./ConnectionsScreen";
import { connectionErrorMessage, readConnectionResult, scopeLabels } from "./text";

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

const DRIVE = "https://www.googleapis.com/auth/drive.readonly";
const SCOPES = [
  "openid",
  "https://www.googleapis.com/auth/calendar.events",
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.compose",
];

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

describe("문서 권한 (ADR-0020)", () => {
  it("권한이 없으면 상태와 '문서 권한 추가' 버튼을 보이고, 누르면 받은 주소로 이동한다", async () => {
    const fetchMock = stubApi((url) =>
      url === "/api/connections/c1/scopes/docs/start"
        ? json({ authorization_url: "https://accounts.example.test/auth?scope=drive" })
        : json(listing([connection()])),
    );
    const navigate = vi.fn();
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={navigate} />);
    expect(await screen.findByText("문서 권한: 없음")).toBeInTheDocument();
    expect(screen.queryByText("Drive 파일 읽기")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "문서 권한 추가" }));
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("https://accounts.example.test/auth?scope=drive");
    });
    const start = (fetchMock.mock.calls as [string, RequestInit | undefined][]).find(
      ([url]) => url === "/api/connections/c1/scopes/docs/start",
    );
    expect(start?.[1]?.method).toBe("POST");
    expect((start?.[1]?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-test-value");
  });

  it("권한이 있으면 허용됨으로 보이고 버튼 없이 허용 권한 목록에 'Drive 파일 읽기'가 들어간다", async () => {
    stubApi(() => json(listing([connection({ docs_granted: true, scopes: [...SCOPES, DRIVE] })])));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByText("문서 권한: 허용됨")).toBeInTheDocument();
    expect(screen.getByText("Drive 파일 읽기")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "문서 권한 추가" })).toBeNull();
    expect(screen.queryByText(/googleapis\.com/)).toBeNull();
  });

  it("docs_granted가 없는 응답은 권한이 없는 것으로 본다", async () => {
    const legacy: Record<string, unknown> = connection();
    delete legacy.docs_granted;
    stubApi(() => json(listing([legacy])));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByText("문서 권한: 없음")).toBeInTheDocument();
  });

  it("시작에 실패하면 이유와 다음 행동을 알리고 다시 누를 수 있다", async () => {
    stubApi((url) =>
      url === "/api/connections/c1/scopes/docs/start"
        ? json({ detail: "unavailable" }, 503)
        : json(listing([connection()])),
    );
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "문서 권한 추가" }));
    expect(await screen.findByText(/문서 권한 추가를 시작하지 못했습니다/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "문서 권한 추가" })).toBeEnabled();
  });

  it.each([
    ["docs_already_granted", "문서 권한이 이미 허용되어 있습니다", true],
    ["connection_inactive", "이 연결로는 문서 권한을 추가할 수 없습니다", false],
  ])("시작이 409 %s면 목록을 다시 읽고 상황에 맞게 알린다", async (detail, phrase, granted) => {
    let started = false;
    const fetchMock = stubApi((url) => {
      if (url === "/api/connections/c1/scopes/docs/start") {
        started = true;
        return json({ detail }, 409);
      }
      return json(
        listing([
          started && granted
            ? connection({ docs_granted: true, scopes: [...SCOPES, DRIVE] })
            : connection(),
        ]),
      );
    });
    const navigate = vi.fn();
    render(<ConnectionsScreen result={null} onBack={() => undefined} navigate={navigate} />);
    await userEvent.click(await screen.findByRole("button", { name: "문서 권한 추가" }));
    expect(await screen.findByText(new RegExp(phrase))).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
    const lists = (fetchMock.mock.calls as [string][]).filter(([u]) => u === "/api/connections");
    expect(lists.length).toBeGreaterThanOrEqual(2); // 처음 + 다시 읽기
    if (granted) expect(await screen.findByText("문서 권한: 허용됨")).toBeInTheDocument();
    expect(screen.queryByText(/시작하지 못했습니다/)).toBeNull();
  });

  it("서버에 연결 설정이 없으면 버튼을 막고, 연결에 문제가 있으면 문서 영역을 보이지 않는다", async () => {
    stubApi(() => json(listing([connection()], { connect_available: false })));
    const { unmount } = render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByRole("button", { name: "문서 권한 추가" })).toBeDisabled();
    unmount();
    stubApi(() => json(listing([connection({ status: "error" })])));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByText(/연결에 문제가 있습니다/)).toBeInTheDocument();
    expect(screen.queryByText("문서 권한: 없음")).toBeNull();
  });

  it("권한 추가를 마치고 돌아오면 성공 안내를 보인다", async () => {
    stubApi(() => json(listing([connection({ docs_granted: true, scopes: [...SCOPES, DRIVE] })])));
    expect(readConnectionResult("?connection=docs_added")).toEqual({ kind: "docs_added" });
    render(
      <ConnectionsScreen
        result={readConnectionResult("?connection=docs_added")}
        onBack={() => undefined}
      />,
    );
    expect(await screen.findByText(/문서 권한을 추가했습니다/)).toBeInTheDocument();
  });

  it.each([
    ["docs_scope_missing", "문서 권한이 허용되지 않아"],
    ["account_mismatch", "다른 Google 계정을 골랐습니다"],
    ["connection_inactive", "문서 권한을 추가할 연결이 없습니다"],
  ])("권한 추가 오류 종류 %s는 고정 문구로 알린다", async (kind, phrase) => {
    expect(connectionErrorMessage(kind)).toContain(phrase);
    stubApi(() => json(listing([connection()])));
    render(<ConnectionsScreen result={{ kind: "error", error: kind }} onBack={() => undefined} />);
    expect(await screen.findByText(new RegExp(phrase))).toBeInTheDocument();
  });

  it("모르는 오류 종류는 주소의 값을 화면에 옮기지 않고 일반 문구를 쓴다", () => {
    expect(connectionErrorMessage("<b>x</b>")).toBe(
      "연결하지 못했습니다. 버튼을 눌러 다시 시도해 주세요.",
    );
    expect(scopeLabels([DRIVE])).toEqual(["Drive 파일 읽기"]);
  });

  it("앱에서 권한 추가 결과 주소로 돌아오면 연결 화면에 안내하고 주소에서 지운다", async () => {
    window.history.replaceState(null, "", "/?connection=docs_added");
    stubApi((url) =>
      url === "/api/session"
        ? json({
            authenticated: true,
            workspace_id: "w1",
            expires_at: "2026-11-13T00:00:00Z",
            display_name: "앨리스 테스트",
            email: "alice@example.test",
            dev_login: true,
            google_login: true,
          })
        : json(listing([connection({ docs_granted: true, scopes: [...SCOPES, DRIVE] })])),
    );
    render(<App />);
    expect(await screen.findByText(/문서 권한을 추가했습니다/)).toBeInTheDocument();
    expect(window.location.search).toBe("");
  });
});
