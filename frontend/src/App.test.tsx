import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { PRODUCT_NAME } from "./brand";

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

const methods = { dev_login: true, google_login: true };
const signedOut = {
  authenticated: false,
  workspace_id: null,
  expires_at: null,
  display_name: null,
  email: null,
  ...methods,
};
const signedIn = {
  authenticated: true,
  workspace_id: "w1",
  expires_at: "2026-11-12T00:00:00Z",
  display_name: "앨리스 테스트",
  email: "alice@example.test",
  ...methods,
};

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("로그인 화면", () => {
  it("로고·개발용 로그인 버튼을 보이고 오류 문구는 비어 있다", async () => {
    stubApi(() => json(signedOut));
    render(<App />);
    expect(await screen.findByRole("button", { name: "개발용 로그인" })).toBeEnabled();
    expect(screen.getByRole("img", { name: PRODUCT_NAME })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeEmptyDOMElement();
  });

  it("Google로 로그인은 서버 시작 주소로 가는 일반 링크다", async () => {
    stubApi(() => json(signedOut));
    render(<App />);
    const link = await screen.findByRole("link", { name: "Google로 로그인" });
    expect(link).toHaveAttribute("href", "/api/auth/google/start");
  });

  it("서버가 개발용 로그인을 열지 않았으면 그 버튼을 보이지 않는다", async () => {
    stubApi(() => json({ ...signedOut, dev_login: false }));
    render(<App />);
    expect(await screen.findByRole("link", { name: "Google로 로그인" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "개발용 로그인" })).toBeNull();
  });

  it("Google 로그인이 닫혀 있으면 링크를 보이지 않는다", async () => {
    stubApi(() => json({ ...signedOut, google_login: false }));
    render(<App />);
    expect(await screen.findByRole("button", { name: "개발용 로그인" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Google로 로그인" })).toBeNull();
  });

  it("로그인 수단이 하나도 없으면 설정 확인을 안내한다", async () => {
    stubApi(() => json({ ...signedOut, dev_login: false, google_login: false }));
    render(<App />);
    expect(await screen.findByText(/사용할 수 있는 로그인 방법이 없습니다/)).toBeInTheDocument();
  });

  it("login_error가 있으면 문구를 알리고 주소에서 지운다", async () => {
    window.history.replaceState(null, "", "/?login_error=not_allowed");
    stubApi(() => json(signedOut));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("허용된 계정으로 로그인해 주세요");
    expect(window.location.search).toBe("");
    expect(screen.getByRole("link", { name: "Google로 로그인" })).toBeInTheDocument();
  });

  it("모르는 login_error 값은 일반 문구로 보이고 값을 화면에 옮기지 않는다", async () => {
    window.history.replaceState(null, "", "/?login_error=%3Cb%3Ex%3C%2Fb%3E");
    stubApi(() => json(signedOut));
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("로그인하지 못했습니다");
    expect(alert).not.toHaveTextContent("<b>");
  });

  it("이미 로그인한 상태면 login_error를 보이지 않는다", async () => {
    window.history.replaceState(null, "", "/?login_error=state");
    stubApi(() => json(signedIn));
    render(<App />);
    expect(await screen.findByRole("button", { name: "로그아웃" })).toBeInTheDocument();
    expect(screen.queryByText(/로그인 정보를 확인하지 못했습니다/)).toBeNull();
  });

  it("로그인에 실패하면 다음 행동을 알리고 다시 누를 수 있다", async () => {
    stubApi((url) => (url === "/api/session" ? json(signedOut) : json({}, 500)));
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "개발용 로그인" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "로그인하지 못했습니다. 서버가 실행 중인지 확인한 뒤 다시 눌러 주세요.",
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "개발용 로그인" })).toBeEnabled();
    });
  });

  it("로그인에 성공하면 채팅 화면으로 넘어간다", async () => {
    let loggedIn = false;
    stubApi((url) => {
      if (url === "/api/dev/login") {
        loggedIn = true;
        return new Response(null, { status: 204 });
      }
      if (url === "/api/session") return json(loggedIn ? signedIn : signedOut);
      return json({}, 500);
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "개발용 로그인" }));
    expect(await screen.findByRole("button", { name: "로그아웃" })).toBeInTheDocument();
    expect(screen.getByLabelText("메시지")).toBeInTheDocument();
  });

  it("서버에 닿지 못하면 연결 오류와 다시 시도 버튼을 보인다", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network")));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("서버에 연결하지 못했습니다");
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
  });
});

describe("채팅 화면", () => {
  it("빈 대화는 처음 할 일을 안내하고 입력창에는 이름표가 있다", async () => {
    stubApi(() => json(signedIn));
    render(<App />);
    expect(await screen.findByText(/물어보세요/)).toBeInTheDocument();
    const input = screen.getByLabelText("메시지");
    expect(input).toHaveAccessibleDescription(/Enter로 전송하고/);
    expect(screen.getByRole("log", { name: "대화" })).toBeInTheDocument();
  });

  it("전송에 실패하면 메시지 옆에 표시하고 입력을 유지하며 다시 보낼 수 있다", async () => {
    let runAttempts = 0;
    stubApi((url) => {
      if (url === "/api/session") return json(signedIn);
      if (url === "/api/runs") {
        runAttempts += 1;
        return runAttempts === 1
          ? json({}, 500)
          : json({ run_id: "r1", conversation_id: "c1", status: "queued" }, 202);
      }
      return json({}, 404);
    });
    // 실시간 연결은 이 테스트의 대상이 아니다.
    vi.stubGlobal(
      "EventSource",
      class {
        addEventListener = vi.fn();
        close = vi.fn();
        onerror: unknown = null;
      },
    );
    render(<App />);
    const input = await screen.findByLabelText("메시지");
    await userEvent.type(input, "내일 일정 알려 줘{Enter}");

    expect(await screen.findByText("전송하지 못했습니다")).toBeInTheDocument();
    expect(screen.getByText(/메시지를 전송하지 못했습니다/)).toBeInTheDocument();
    expect(input).toHaveValue("내일 일정 알려 줘");
    expect(input).toHaveAccessibleDescription(/메시지를 전송하지 못했습니다/);

    await userEvent.click(screen.getByRole("button", { name: "다시 전송" }));
    await waitFor(() => {
      expect(screen.queryByText("전송하지 못했습니다")).toBeNull();
    });
    expect(input).toHaveValue("");
    expect(runAttempts).toBe(2);
    // 접수된 메시지는 일반 사용자 메시지로 남는다.
    expect(screen.getByText("내일 일정 알려 줘")).toBeInTheDocument();
  });
});
