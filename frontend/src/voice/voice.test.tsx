import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { TRUNCATION_NOTICE } from "./speakable";
import { FakeUtterance, installFakeSpeech } from "./speechTestSupport";
import { installFakeMic } from "./voiceTestSupport";

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
  constructor(readonly url: string) {
    super();
    FakeSource.instances.push(this);
  }
  close() {
    this.readyState = 2;
  }
  emit(type: string, seq: number, data: unknown) {
    this.dispatchEvent(
      new MessageEvent(type, { data: JSON.stringify(data), lastEventId: String(seq) }),
    );
  }
}

interface Calls {
  runBodies: Record<string, unknown>[];
  transcribeBodies: BodyInit[];
}

let calls: Calls;
let transcript = "내일 오전 일정 알려 줘";
let mic: ReturnType<typeof installFakeMic>;
let synth: ReturnType<typeof installFakeSpeech>;

function stubApi() {
  calls = { runBodies: [], transcribeBodies: [] };
  const handler = (url: string, init?: RequestInit): Response => {
    if (url === "/api/session") return json(signedIn);
    if (url.startsWith("/api/conversations?")) return json({ conversations: [] });
    if (url === "/api/voice/transcribe") {
      calls.transcribeBodies.push(init?.body ?? "");
      return json({ text: transcript });
    }
    if (url === "/api/runs") {
      calls.runBodies.push(
        JSON.parse(typeof init?.body === "string" ? init.body : "{}") as Record<string, unknown>,
      );
      return json({ run_id: "rn", conversation_id: "cn", status: "queued" }, 202);
    }
    return json({}, 404);
  };
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => Promise.resolve(handler(url, init))),
  );
  vi.stubGlobal("EventSource", FakeSource);
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: true,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

beforeEach(() => {
  transcript = "내일 오전 일정 알려 줘";
  stubApi();
  mic = installFakeMic();
  synth = installFakeSpeech();
});

afterEach(() => {
  FakeSource.instances = [];
  mic.restore(); // unstubAllGlobals도 함께 한다
  window.localStorage.clear();
});

const micButton = () => screen.getByRole("button", { name: /음성 입력/ });
const input = () => screen.getByLabelText("메시지");

async function dictate() {
  await userEvent.click(micButton());
  await waitFor(() => {
    expect(micButton()).toHaveAttribute("aria-pressed", "true");
  });
  await userEvent.click(micButton());
  await waitFor(() => {
    expect(micButton()).toHaveAttribute("aria-pressed", "false");
    expect(micButton()).toHaveAttribute("aria-disabled", "false");
  });
}

async function answer(text: string, status = "succeeded") {
  await waitFor(() => {
    expect(FakeSource.instances).toHaveLength(1);
  });
  act(() => {
    FakeSource.instances[0]?.emit("status", 1, { status: "running" });
    FakeSource.instances[0]?.emit("assistant_text", 2, { step: 0, text });
    FakeSource.instances[0]?.emit("status", 3, { status });
  });
}

describe("음성 입력 (입력창)", () => {
  it("전사 결과를 입력창에 넣고 자동으로 보내지 않는다", async () => {
    render(<App />);
    await screen.findByLabelText("메시지");
    await dictate();
    await waitFor(() => {
      expect(input()).toHaveValue("내일 오전 일정 알려 줘");
    });
    expect(calls.runBodies).toHaveLength(0);
    const body = calls.transcribeBodies[0];
    expect(body).toBeInstanceOf(FormData);
    expect((body as FormData).get("audio")).toBeInstanceOf(Blob);
  });

  it("입력창에 내용이 있으면 뒤에 덧붙인다", async () => {
    render(<App />);
    await screen.findByLabelText("메시지");
    await userEvent.type(input(), "그리고");
    await dictate();
    await waitFor(() => {
      expect(input()).toHaveValue("그리고 내일 오전 일정 알려 줘");
    });
  });

  it("마이크가 없는 환경에서는 버튼도 자동 읽기 설정도 보이지 않는다", async () => {
    mic.restore();
    stubApi();
    installFakeSpeech();
    render(<App />);
    await screen.findByLabelText("메시지");
    expect(screen.queryByRole("button", { name: /음성 입력/ })).toBeNull();
    expect(screen.queryByLabelText("음성으로 질문하면 답을 자동으로 읽기")).toBeNull();
  });

  it("마이크 권한이 거부되면 안내 문구를 알린다", async () => {
    mic.restore();
    stubApi();
    mic = installFakeMic({ error: new DOMException("denied", "NotAllowedError") });
    installFakeSpeech();
    render(<App />);
    await screen.findByLabelText("메시지");
    await userEvent.click(micButton());
    const message = await screen.findByText(/마이크 권한이 없습니다/);
    expect(message.closest("[role=alert]")).not.toBeNull();
  });
});

describe("읽어 주기", () => {
  async function sendTyped(text: string) {
    await userEvent.type(input(), `${text}{Enter}`);
    await waitFor(() => {
      expect(calls.runBodies).toHaveLength(1);
    });
  }

  it("비서 답마다 버튼이 있고 누르면 정리한 문장을 읽으며 다시 누르면 멈춘다", async () => {
    render(<App />);
    await screen.findByLabelText("메시지");
    await sendTyped("일정");
    await answer("**회의(E1)** 는 [자료](https://example.com/x) 참고");
    const log = screen.getByRole("log", { name: "대화" });
    await userEvent.click(await within(log).findByRole("button", { name: "읽어 주기" }));
    expect(FakeUtterance.instances[0]?.text).toBe("회의 는 자료 참고");
    expect(synth.speak).toHaveBeenCalledOnce();
    await userEvent.click(within(log).getByRole("button", { name: "읽기 멈추기" }));
    expect(synth.cancel).toHaveBeenCalled();
    expect(within(log).getByRole("button", { name: "읽어 주기" })).toBeInTheDocument();
    // 사용자 메시지에는 읽어 주기 버튼이 없다.
    expect(within(log).getAllByRole("button", { name: /읽어 주기|읽기 멈추기/ })).toHaveLength(1);
  });

  it("긴 답은 600자까지만 읽고 안내 문구를 붙인다", async () => {
    render(<App />);
    await screen.findByLabelText("메시지");
    await sendTyped("길게");
    await answer("가".repeat(700));
    await userEvent.click(await screen.findByRole("button", { name: "읽어 주기" }));
    const spoken = FakeUtterance.instances[0]?.text ?? "";
    expect(spoken.endsWith(TRUNCATION_NOTICE)).toBe(true);
    expect(spoken.startsWith("가".repeat(600))).toBe(true);
  });

  it("읽을 문장이 남지 않는 답에는 버튼을 그리지 않는다", async () => {
    render(<App />);
    await screen.findByLabelText("메시지");
    await sendTyped("코드");
    await answer("```\nsecret()\n```");
    await waitFor(() => {
      expect(screen.getByRole("log", { name: "대화" })).toHaveTextContent("secret()");
    });
    expect(screen.queryByRole("button", { name: "읽어 주기" })).toBeNull();
  });

  it("speechSynthesis가 없으면 읽어 주기 버튼도 자동 읽기 설정도 없다", async () => {
    vi.stubGlobal("SpeechSynthesisUtterance", undefined);
    render(<App />);
    await screen.findByLabelText("메시지");
    await sendTyped("일정");
    await answer("답");
    await screen.findByText("답");
    expect(screen.queryByRole("button", { name: "읽어 주기" })).toBeNull();
    expect(screen.queryByLabelText("음성으로 질문하면 답을 자동으로 읽기")).toBeNull();
  });

  it("승인 대기 중에는 읽지 않는다", async () => {
    window.localStorage.setItem("orbit.voice.autoRead", "1");
    render(<App />);
    await screen.findByLabelText("메시지");
    await dictate();
    await waitFor(() => {
      expect(input()).toHaveValue(transcript);
    });
    await userEvent.type(input(), "{Enter}");
    await waitFor(() => {
      expect(calls.runBodies).toHaveLength(1);
    });
    await waitFor(() => {
      expect(FakeSource.instances).toHaveLength(1);
    });
    act(() => {
      FakeSource.instances[0]?.emit("assistant_text", 1, { step: 0, text: "메일을 보낼까요?" });
      FakeSource.instances[0]?.emit("approval_requested", 2, {
        approval_id: "a1",
        tool_name: "send_mail",
        summary: {},
      });
      FakeSource.instances[0]?.emit("status", 3, { status: "waiting_approval" });
    });
    await screen.findByText("메일을 보낼까요?");
    expect(synth.speak).not.toHaveBeenCalled();
  });
});

describe("음성 질문 자동 읽기", () => {
  const autoReadBox = () => screen.getByLabelText("음성으로 질문하면 답을 자동으로 읽기");

  it("기본은 꺼짐이고 켜면 저장한다", async () => {
    render(<App />);
    await screen.findByLabelText("메시지");
    expect(autoReadBox()).not.toBeChecked();
    await userEvent.click(autoReadBox());
    expect(autoReadBox()).toBeChecked();
    expect(window.localStorage.getItem("orbit.voice.autoRead")).toBe("1");
  });

  it("저장소를 쓸 수 없어도 화면은 동작한다", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    render(<App />);
    await screen.findByLabelText("메시지");
    expect(autoReadBox()).not.toBeChecked();
    await userEvent.click(autoReadBox());
    expect(autoReadBox()).toBeChecked();
    vi.restoreAllMocks();
  });

  it("켠 상태에서 음성으로 질문하면 답이 끝났을 때 마지막 답을 읽는다", async () => {
    window.localStorage.setItem("orbit.voice.autoRead", "1");
    render(<App />);
    await screen.findByLabelText("메시지");
    await dictate();
    await waitFor(() => {
      expect(input()).toHaveValue(transcript);
    });
    await userEvent.type(input(), "{Enter}");
    await waitFor(() => {
      expect(calls.runBodies).toHaveLength(1);
    });
    expect(synth.speak).not.toHaveBeenCalled(); // 답이 오기 전에는 읽지 않는다
    await answer("내일 오전에는 **회의**가 있습니다 (E1).");
    await waitFor(() => {
      expect(synth.speak).toHaveBeenCalledOnce();
    });
    expect(FakeUtterance.instances[0]?.text).toBe("내일 오전에는 회의가 있습니다.");
  });

  it("글로 질문하면 켜 두어도 읽지 않는다", async () => {
    window.localStorage.setItem("orbit.voice.autoRead", "1");
    render(<App />);
    await screen.findByLabelText("메시지");
    await userEvent.type(input(), "일정 알려 줘{Enter}");
    await waitFor(() => {
      expect(calls.runBodies).toHaveLength(1);
    });
    await answer("답입니다");
    await screen.findByText("답입니다");
    expect(synth.speak).not.toHaveBeenCalled();
  });

  it("꺼 둔 상태에서는 음성으로 질문해도 읽지 않는다", async () => {
    render(<App />);
    await screen.findByLabelText("메시지");
    await dictate();
    await waitFor(() => {
      expect(input()).toHaveValue(transcript);
    });
    await userEvent.type(input(), "{Enter}");
    await waitFor(() => {
      expect(calls.runBodies).toHaveLength(1);
    });
    await answer("답입니다");
    await screen.findByText("답입니다");
    expect(synth.speak).not.toHaveBeenCalled();
  });

  it("전사 뒤 입력창을 비우고 새로 쓴 질문은 음성 질문으로 보지 않는다", async () => {
    window.localStorage.setItem("orbit.voice.autoRead", "1");
    render(<App />);
    await screen.findByLabelText("메시지");
    await dictate();
    await waitFor(() => {
      expect(input()).toHaveValue(transcript);
    });
    await userEvent.clear(input());
    await userEvent.type(input(), "직접 쓴 질문{Enter}");
    await waitFor(() => {
      expect(calls.runBodies).toHaveLength(1);
    });
    await answer("답입니다");
    await screen.findByText("답입니다");
    expect(synth.speak).not.toHaveBeenCalled();
  });
});
