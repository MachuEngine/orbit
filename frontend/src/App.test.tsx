import { act, render, screen, waitFor } from "@testing-library/react";
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

/** 테스트용 EventSource. `fail()`은 브라우저가 재접속을 포기한 상태(CLOSED)를 만든다. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readyState = 1;
  onerror: (() => void) | null = null;
  addEventListener = vi.fn();
  close = vi.fn(() => {
    this.readyState = 2;
  });
  constructor() {
    FakeEventSource.instances.push(this);
  }
  fail() {
    this.readyState = 2;
    this.onerror?.();
  }
}

afterEach(() => {
  FakeEventSource.instances = [];
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

describe("알림", () => {
  const arrived = {
    notifications: [
      {
        id: "n1",
        kind: "reminder",
        title: "합성 보고서 제출",
        due_at: "2026-11-12T01:00:00Z",
        event_start: null,
        read: false,
        created_at: "2026-11-12T00:00:00Z",
      },
    ],
    unread_count: 1,
  };

  it("채팅 헤더에 읽지 않은 수가 이름에 들어간 알림 버튼을 보인다", async () => {
    stubApi((url) => (url === "/api/notifications" ? json(arrived) : json(signedIn)));
    render(<App />);
    expect(await screen.findByRole("button", { name: "알림 1개" })).toBeInTheDocument();
  });

  it("알림을 읽다가 로그인이 끝났으면(401) 세션을 다시 확인해 로그인 화면으로 돌아간다", async () => {
    let sessionCalls = 0;
    stubApi((url) => {
      if (url === "/api/notifications") return json({ detail: "unauthorized" }, 401);
      if (url === "/api/session") {
        sessionCalls += 1;
        return json(sessionCalls === 1 ? signedIn : signedOut);
      }
      return json({}, 500);
    });
    render(<App />);
    expect(await screen.findByRole("button", { name: "개발용 로그인" })).toBeInTheDocument();
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

  it("재시도는 같은 Idempotency-Key를 쓰고, 성공한 뒤 새 메시지는 새 키를 쓴다", async () => {
    const keys: (string | undefined)[] = [];
    stubApi((url, init) => {
      if (url === "/api/session") return json(signedIn);
      if (url === "/api/runs") {
        keys.push(new Headers(init?.headers).get("Idempotency-Key") ?? undefined);
        return keys.length === 1
          ? json({}, 500)
          : json({ run_id: `r${keys.length}`, conversation_id: "c1", status: "queued" }, 202);
      }
      return json({}, 404);
    });
    vi.stubGlobal("EventSource", FakeEventSource);
    render(<App />);
    const input = await screen.findByLabelText("메시지");
    await userEvent.type(input, "첫 메시지{Enter}");
    await userEvent.click(await screen.findByRole("button", { name: "다시 전송" }));
    await waitFor(() => {
      expect(input).toHaveValue("");
    });
    expect(keys).toHaveLength(2);
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);
    expect(keys[1]).toBe(keys[0]);

    await userEvent.type(input, "둘째 메시지{Enter}");
    await waitFor(() => {
      expect(keys).toHaveLength(3);
    });
    expect(keys[2]).toMatch(/^[0-9a-f-]{36}$/);
    expect(keys[2]).not.toBe(keys[0]);
  });

  it("같은 키로 다른 본문이라는 409는 새로 보내라고 안내하고 키를 버린다", async () => {
    const keys: (string | null)[] = [];
    stubApi((url, init) => {
      if (url === "/api/session") return json(signedIn);
      if (url === "/api/runs") {
        keys.push(new Headers(init?.headers).get("Idempotency-Key"));
        return keys.length === 1
          ? json({ detail: "idempotency_conflict" }, 409)
          : json({ run_id: "r2", conversation_id: "c1", status: "queued" }, 202);
      }
      return json({}, 404);
    });
    vi.stubGlobal("EventSource", FakeEventSource);
    render(<App />);
    const input = await screen.findByLabelText("메시지");
    await userEvent.type(input, "안녕{Enter}");
    expect(await screen.findByText(/같은 요청을 다시 보내지 못했습니다/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "다시 전송" }));
    await waitFor(() => {
      expect(keys).toHaveLength(2);
    });
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("실시간 연결이 닫히면 세션을 다시 확인해 만료됐으면 로그인 화면으로 돌아간다", async () => {
    let expired = false;
    stubApi((url) => {
      if (url === "/api/session") return json(expired ? signedOut : signedIn);
      if (url === "/api/runs") {
        return json({ run_id: "r1", conversation_id: "c1", status: "queued" }, 202);
      }
      return json({}, 404);
    });
    vi.stubGlobal("EventSource", FakeEventSource);
    render(<App />);
    await userEvent.type(await screen.findByLabelText("메시지"), "안녕{Enter}");
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
    expired = true;
    act(() => {
      FakeEventSource.instances[0]?.fail(); // 서버가 닫았고 재접속은 401이라 브라우저가 포기한 상태
    });
    expect(await screen.findByRole("button", { name: "개발용 로그인" })).toBeInTheDocument();
    expect(FakeEventSource.instances[0]?.readyState).toBe(2);
  });

  it("연결이 닫힌 뒤 다시 렌더링해도 세션 확인은 한 번만 한다", async () => {
    let sessionCalls = 0;
    stubApi((url) => {
      if (url === "/api/session") {
        sessionCalls += 1;
        return json(signedIn);
      }
      if (url === "/api/runs") {
        return json({ run_id: "r1", conversation_id: "c1", status: "succeeded" }, 200);
      }
      return json({}, 404);
    });
    vi.stubGlobal("EventSource", FakeEventSource);
    render(<App />);
    const input = await screen.findByLabelText("메시지");
    expect(screen.getByRole("heading", { level: 1, name: `${PRODUCT_NAME} 대화` })).toBeVisible();
    await userEvent.type(input, "안녕{Enter}");
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
    const before = sessionCalls;
    act(() => {
      FakeEventSource.instances[0]?.fail();
    });
    await screen.findByText(/연결이 끊겼습니다/);
    await userEvent.type(input, "다음 입력"); // 입력할 때마다 다시 렌더링된다
    await waitFor(() => {
      expect(sessionCalls).toBe(before + 1);
    });
    expect(sessionCalls).toBe(before + 1);
  });

  it("실시간 연결이 닫혀도 세션이 살아 있으면 채팅 화면에 머문다", async () => {
    stubApi((url) => {
      if (url === "/api/session") return json(signedIn);
      if (url === "/api/runs") {
        return json({ run_id: "r1", conversation_id: "c1", status: "queued" }, 202);
      }
      return json({}, 404);
    });
    vi.stubGlobal("EventSource", FakeEventSource);
    render(<App />);
    await userEvent.type(await screen.findByLabelText("메시지"), "안녕{Enter}");
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
    act(() => {
      FakeEventSource.instances[0]?.fail();
    });
    expect(await screen.findByText(/연결이 끊겼습니다/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "로그아웃" })).toBeInTheDocument();
  });

  it("로그아웃하면 열려 있던 실시간 연결을 닫는다", async () => {
    stubApi((url) => {
      if (url === "/api/session") return json(signedIn);
      if (url === "/api/runs") {
        return json({ run_id: "r1", conversation_id: "c1", status: "queued" }, 202);
      }
      if (url === "/api/logout") return new Response(null, { status: 204 });
      return json({}, 404);
    });
    vi.stubGlobal("EventSource", FakeEventSource);
    render(<App />);
    await userEvent.type(await screen.findByLabelText("메시지"), "안녕{Enter}");
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
    await userEvent.click(screen.getByRole("button", { name: "로그아웃" }));
    expect(await screen.findByRole("button", { name: "개발용 로그인" })).toBeInTheDocument();
    expect(FakeEventSource.instances[0]?.close).toHaveBeenCalled();
  });
});
