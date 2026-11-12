import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const signedIn = {
  authenticated: true,
  workspace_id: "w1",
  expires_at: "2026-11-12T00:00:00Z",
  display_name: "앨리스 테스트",
  email: "alice@example.test",
  dev_login: true,
  google_login: true,
};

class FakeSource extends EventTarget {
  static instances: FakeSource[] = [];
  readyState = 1;
  onerror: (() => void) | null = null;
  readonly url: string;
  closed = false;
  constructor(url: string) {
    super();
    this.url = url;
    FakeSource.instances.push(this);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  emit(type: string, seq: number, data: unknown) {
    this.dispatchEvent(
      new MessageEvent(type, { data: JSON.stringify(data), lastEventId: String(seq) }),
    );
  }
}

/** 화면 너비 조건을 흉내 낸다. jsdom에는 `matchMedia`가 없다. */
function stubViewport(wide: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      matches: wide,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

const hoursAgo = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString();

const summaries = [
  {
    id: "c2",
    title: "내일 일정 알려 줘",
    created_at: hoursAgo(3),
    last_activity_at: hoursAgo(2),
    last_run_status: "succeeded",
  },
  {
    id: "c1",
    title: null,
    created_at: hoursAgo(30),
    last_activity_at: hoursAgo(30),
    last_run_status: "failed",
  },
];

const turn = (overrides: Record<string, unknown>) => ({
  run_id: "r1",
  status: "succeeded",
  user_text: "질문",
  assistant_text: "답",
  pending_approval_id: null,
  created_at: hoursAgo(3),
  ...overrides,
});

interface Calls {
  runBodies: Record<string, unknown>[];
  listCalls: number;
}

function stubApi(options: {
  details?: Record<string, unknown>;
  list?: () => Response;
  runs?: (body: Record<string, unknown>) => Response;
  detail?: (id: string) => Response | Promise<Response>;
}): Calls {
  const calls: Calls = { runBodies: [], listCalls: 0 };
  const handler: Handler = (url, init) => {
    if (url === "/api/session") return json(signedIn);
    if (url.startsWith("/api/conversations?")) {
      calls.listCalls += 1;
      return options.list?.() ?? json({ conversations: summaries });
    }
    if (url.startsWith("/api/conversations/")) {
      const id = decodeURIComponent(url.slice("/api/conversations/".length));
      if (options.detail) return options.detail(id);
      const detail = options.details?.[id];
      return detail === undefined ? json({ detail: "not_found" }, 404) : json(detail);
    }
    if (url === "/api/runs") {
      const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as Record<
        string,
        unknown
      >;
      calls.runBodies.push(body);
      return (
        options.runs?.(body) ?? json({ run_id: "rn", conversation_id: "cn", status: "queued" }, 202)
      );
    }
    return json({}, 404);
  };
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation((url: string, init?: RequestInit) => Promise.resolve(handler(url, init))),
  );
  vi.stubGlobal("EventSource", FakeSource);
  return calls;
}

afterEach(() => {
  FakeSource.instances = [];
  vi.unstubAllGlobals();
  document.body.className = "";
});

describe("대화 목록 (넓은 화면)", () => {
  it("제목·마지막 활동 시각을 보이고 제목이 없으면 기본 문구를 쓴다", async () => {
    stubViewport(true);
    stubApi({});
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "대화 목록" });
    expect(await within(nav).findByRole("button", { name: /내일 일정 알려 줘/ })).toHaveTextContent(
      "2시간 전",
    );
    expect(within(nav).getByRole("button", { name: /제목 없는 대화/ })).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: "새 대화" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "대화 목록" })).toBeNull();
  });

  it("목록이 비면 안내하고, 불러오지 못하면 다시 불러올 수 있다", async () => {
    stubViewport(true);
    let fail = true;
    stubApi({
      list: () => (fail ? json({}, 500) : json({ conversations: [] })),
    });
    render(<App />);
    expect(await screen.findByText("대화 목록을 불러오지 못했습니다.")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "다시 불러오기" }));
    expect(await screen.findByText(/아직 대화가 없습니다/)).toBeInTheDocument();
  });

  it("항목을 고르면 턴을 채우고 현재 대화로 표시하며, 이어서 보내면 conversation_id를 포함한다", async () => {
    stubViewport(true);
    const calls = stubApi({
      details: {
        c2: {
          id: "c2",
          turns: [
            turn({ run_id: "r1", user_text: "내일 일정 알려 줘", assistant_text: "**회의** 2건" }),
          ],
        },
      },
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    const log = screen.getByRole("log", { name: "대화" });
    expect(await within(log).findByText("내일 일정 알려 줘")).toBeInTheDocument();
    expect(within(log).getByText("회의")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /내일 일정 알려 줘/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(FakeSource.instances).toHaveLength(0); // 끝난 대화는 구독하지 않는다

    await userEvent.type(screen.getByLabelText("메시지"), "그거 3시로{Enter}");
    await waitFor(() => {
      expect(calls.runBodies).toHaveLength(1);
    });
    expect(calls.runBodies[0]).toMatchObject({ message: "그거 3시로", conversation_id: "c2" });
  });

  it("새 대화는 화면을 비우고 conversation_id 없이 보낸다", async () => {
    stubViewport(true);
    const calls = stubApi({
      details: { c2: { id: "c2", turns: [turn({ user_text: "이전 질문" })] } },
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    await screen.findByText("이전 질문");
    await userEvent.click(screen.getByRole("button", { name: "새 대화" }));
    expect(screen.queryByText("이전 질문")).toBeNull();
    expect(screen.getByText(/물어보세요/)).toBeInTheDocument();
    expect(screen.getByLabelText("메시지")).toHaveFocus();
    for (const item of screen.getAllByRole("button", { name: /대화/ })) {
      expect(item).not.toHaveAttribute("aria-current");
    }
    await userEvent.type(screen.getByLabelText("메시지"), "새 질문{Enter}");
    await waitFor(() => {
      expect(calls.runBodies).toHaveLength(1);
    });
    expect(calls.runBodies[0]).toEqual({ message: "새 질문" });
  });

  it("run을 만든 뒤와 run이 끝난 뒤 목록을 다시 불러온다", async () => {
    stubViewport(true);
    const calls = stubApi({});
    render(<App />);
    await screen.findByRole("button", { name: /내일 일정 알려 줘/ });
    expect(calls.listCalls).toBe(1);
    await userEvent.type(screen.getByLabelText("메시지"), "안녕{Enter}");
    await waitFor(() => {
      expect(calls.listCalls).toBe(2);
    });
    await waitFor(() => {
      expect(FakeSource.instances).toHaveLength(1);
    });
    act(() => {
      FakeSource.instances[0]?.emit("status", 1, { status: "succeeded" });
    });
    await waitFor(() => {
      expect(calls.listCalls).toBe(3);
    });
  });

  it("대화를 불러오지 못하면 현재 화면을 유지하고 안내한다", async () => {
    stubViewport(true);
    stubApi({
      details: { c2: { id: "c2", turns: [turn({ user_text: "보이는 질문" })] } },
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    await screen.findByText("보이는 질문");
    await userEvent.click(screen.getByRole("button", { name: /제목 없는 대화/ })); // c1은 404
    expect(await screen.findByText(/이 대화를 찾을 수 없습니다/)).toBeInTheDocument();
    expect(screen.getByText("보이는 질문")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /내일 일정 알려 줘/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });
});

describe("진행 중인 run 이어서 보기", () => {
  const running = {
    id: "c2",
    turns: [
      turn({ run_id: "r1", user_text: "첫 질문", assistant_text: "첫 답" }),
      turn({
        run_id: "r2",
        status: "running",
        user_text: "둘째 질문",
        assistant_text: "이미 받은 부분 답",
      }),
    ],
  };

  it("그 run을 구독해 이어서 보여 주고 이미 받은 응답을 중복 표시하지 않는다", async () => {
    stubViewport(true);
    stubApi({ details: { c2: running } });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    await waitFor(() => {
      expect(FakeSource.instances).toHaveLength(1);
    });
    expect(FakeSource.instances[0]?.url).toBe("/api/runs/r2/events");
    const log = screen.getByRole("log", { name: "대화" });
    expect(within(log).getByText("둘째 질문")).toBeInTheDocument();
    expect(within(log).queryByText("이미 받은 부분 답")).toBeNull();

    // 서버가 처음부터 재생하는 이벤트가 한 번만 반영된다.
    act(() => {
      FakeSource.instances[0]?.emit("status", 1, { status: "running" });
      FakeSource.instances[0]?.emit("assistant_text", 2, { step: 0, text: "이미 받은 부분 답" });
    });
    expect(within(log).getAllByText("이미 받은 부분 답")).toHaveLength(1);
    expect(screen.getByText("작업 중입니다.")).toBeInTheDocument();
  });

  it("승인 대기 중인 run이면 승인 카드를 다시 보여 준다", async () => {
    stubViewport(true);
    const waiting = {
      id: "c2",
      turns: [
        turn({
          run_id: "r2",
          status: "waiting_approval",
          assistant_text: null,
          pending_approval_id: "ap1",
        }),
      ],
    };
    stubApi({ details: { c2: waiting } });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    await waitFor(() => {
      expect(FakeSource.instances).toHaveLength(1);
    });
    act(() => {
      FakeSource.instances[0]?.emit("approval_requested", 1, {
        step: 1,
        approval_id: "ap1",
        tool_name: "send_mail",
        summary: { to_count: 1 },
      });
      FakeSource.instances[0]?.emit("status", 2, { status: "waiting_approval" });
    });
    expect(screen.getByLabelText("메시지")).toHaveAccessibleDescription(/승인 대기 중/);
  });

  it("대화를 바꾸면 열려 있던 구독을 닫는다", async () => {
    stubViewport(true);
    stubApi({
      details: {
        c2: running,
        c1: { id: "c1", turns: [turn({ user_text: "옛 질문" })] },
      },
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    await waitFor(() => {
      expect(FakeSource.instances).toHaveLength(1);
    });
    await userEvent.click(screen.getByRole("button", { name: /제목 없는 대화/ }));
    await screen.findByText("옛 질문");
    expect(FakeSource.instances[0]?.closed).toBe(true);
    expect(FakeSource.instances).toHaveLength(1);
    expect(screen.queryByText("둘째 질문")).toBeNull();
  });
});

describe("대화가 진행 중일 때 보내기", () => {
  it("conversation_busy 409는 끝난 뒤 다시 보내라고 안내한다", async () => {
    stubViewport(true);
    stubApi({
      details: { c2: { id: "c2", turns: [turn({})] } },
      runs: () => json({ detail: "conversation_busy" }, 409),
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    await screen.findByText("답");
    await userEvent.type(screen.getByLabelText("메시지"), "또 질문{Enter}");
    expect(
      await screen.findByText(
        /이 대화에서 아직 진행 중인 작업이 있습니다\. 끝난 뒤 다시 보내 주세요\./,
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("메시지")).toHaveValue("또 질문");
  });

  it("멱등 키 409는 기존 문구를 유지한다", async () => {
    stubViewport(true);
    stubApi({ runs: () => json({ detail: "idempotency_key_reused" }, 409) });
    render(<App />);
    await userEvent.type(await screen.findByLabelText("메시지"), "안녕{Enter}");
    expect(await screen.findByText(/같은 요청을 다시 보내지 못했습니다/)).toBeInTheDocument();
    expect(screen.queryByText(/아직 진행 중인 작업/)).toBeNull();
  });
});

describe("대화 목록 (좁은 화면)", () => {
  it("버튼으로 열면 포커스가 패널로 가고 Esc로 닫으면 버튼으로 돌아온다", async () => {
    stubViewport(false);
    stubApi({});
    render(<App />);
    const toggle = await screen.findByRole("button", { name: "대화 목록" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).toBeNull();

    await userEvent.click(toggle);
    const dialog = screen.getByRole("dialog", { name: "대화 목록" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveFocus();
    expect(document.body).toHaveClass("conversation-scroll-lock");
    expect(screen.getByRole("main", { hidden: true })).toHaveAttribute("inert");
    expect(await within(dialog).findByRole("button", { name: /내일 일정 알려 줘/ })).toBeVisible();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body).not.toHaveClass("conversation-scroll-lock");
    expect(screen.getByRole("main")).not.toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "대화 목록" })).toHaveFocus();
  });

  it("닫기 버튼으로 닫고, 항목을 고르면 불러온 뒤 패널을 닫고 입력창으로 포커스를 준다", async () => {
    stubViewport(false);
    stubApi({ details: { c2: { id: "c2", turns: [turn({ user_text: "옛 질문" })] } } });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "대화 목록" }));
    await userEvent.click(screen.getByRole("button", { name: "닫기" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "대화 목록" }));
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    expect(await screen.findByText("옛 질문")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.getByLabelText("메시지")).toHaveFocus();
  });
});

describe("대화 상자 보완", () => {
  it("Tab과 Shift+Tab이 패널 안에서 순환하고 aria-controls는 열렸을 때만 있다", async () => {
    stubViewport(false);
    stubApi({});
    render(<App />);
    const toggle = await screen.findByRole("button", { name: "대화 목록" });
    expect(toggle).not.toHaveAttribute("aria-controls");
    await userEvent.click(toggle);
    const dialog = screen.getByRole("dialog");
    expect(screen.getByRole("button", { name: "대화 목록", hidden: true })).toHaveAttribute(
      "aria-controls",
      dialog.id,
    );
    const close = within(dialog).getByRole("button", { name: "닫기" });
    await within(dialog).findByRole("button", { name: /내일 일정 알려 줘/ });
    const buttons = within(dialog).getAllByRole("button");
    const last = buttons[buttons.length - 1];
    expect(close).toBe(buttons[0]);
    last?.focus();
    await userEvent.tab();
    expect(close).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(last).toHaveFocus();
  });

  it("대화 상자가 열린 채 화면이 넓어지면 패널을 고정하고 포커스를 입력창에 둔다", async () => {
    let listener: (() => void) | null = null;
    let matches = false;
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        get matches() {
          return matches;
        },
        media: query,
        addEventListener: (_: string, fn: () => void) => {
          listener = fn;
        },
        removeEventListener: vi.fn(),
      })),
    );
    stubApi({});
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "대화 목록" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    matches = true;
    act(() => {
      listener?.();
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("navigation", { name: "대화 목록" })).toBeInTheDocument();
    expect(screen.getByLabelText("메시지")).toHaveFocus();
    expect(document.body).not.toHaveClass("conversation-scroll-lock");
  });
});

describe("실패·취소된 턴 안내", () => {
  it("응답 없는 실패 턴 뒤에 안내를 보이고, 취소는 응답이 없을 때만 보인다", async () => {
    stubViewport(true);
    stubApi({
      details: {
        c2: {
          id: "c2",
          turns: [
            turn({
              run_id: "r1",
              status: "failed",
              user_text: "실패한 질문",
              assistant_text: null,
            }),
            turn({
              run_id: "r2",
              status: "cancelled",
              user_text: "취소한 질문",
              assistant_text: null,
            }),
            turn({
              run_id: "r3",
              status: "cancelled",
              user_text: "일부 답 질문",
              assistant_text: "일부 답",
            }),
          ],
        },
      },
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /내일 일정 알려 줘/ }));
    const log = screen.getByRole("log", { name: "대화" });
    expect(await within(log).findByText("이 요청은 처리하지 못했습니다.")).toBeInTheDocument();
    expect(within(log).getAllByText("이 요청은 취소되었습니다.")).toHaveLength(1);
    expect(within(log).getByText("일부 답")).toBeInTheDocument();
  });
});
