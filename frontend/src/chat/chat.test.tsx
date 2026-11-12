import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { vi } from "vitest";
import { useComposer } from "../composer/useComposer";
import { Composer } from "./Composer";
import { describe, expect, it } from "vitest";
import { initialRunViewState } from "../runs/reducer";
import { MessageList } from "./MessageList";
import { StatusBar } from "./StatusBar";
import {
  connectionStatusItem,
  connectorNoticeItems,
  connectorNoticeText,
  runStatusItem,
} from "./status";
import { isNearBottom } from "./useStickToBottom";

const run = (overrides: Partial<typeof initialRunViewState>) => ({
  ...initialRunViewState,
  ...overrides,
});

describe("runStatusItem", () => {
  it("진행 상태마다 다른 문구를 쓰고 대기 전에는 아무것도 보이지 않는다", () => {
    expect(runStatusItem(run({ status: "idle" }))).toBeNull();
    const texts = (["queued", "waiting_approval", "succeeded", "failed", "cancelled"] as const).map(
      (status) => runStatusItem(run({ status }))?.text,
    );
    expect(texts[0]).toBe("작업 중입니다.");
    expect(texts[1]).toContain("승인 대기 중");
    expect(texts[2]).toBe("완료했습니다.");
    expect(texts[3]).toContain("다시 요청해 주세요");
    expect(new Set(texts).size).toBe(5);
  });

  it("서버 오류 이벤트는 실행 상태가 running이어도 실패로 알린다", () => {
    expect(runStatusItem(run({ status: "running", error: "x" }))?.tone).toBe("error");
  });
});

describe("실패 원인 문구", () => {
  const failed = (error: string | null) => runStatusItem(run({ status: "failed", error }))?.text;

  it("error_kind에 따라 다른 안내를 보인다", () => {
    expect(failed("no_connection")).toContain("계정이 연결되어 있지 않습니다");
    expect(failed("max_steps")).toContain("요청을 나눠서");
    for (const kind of ["LLMError", "TransientLLMError", "LLMTimeoutError", "ModelMismatchError"]) {
      expect(failed(kind)).toContain("모델 호출에 실패했습니다");
    }
    expect(failed("approval_missing")).toContain("작업을 마치지 못했습니다");
    expect(failed(null)).toContain("작업을 마치지 못했습니다");
  });
});

describe("connectorNoticeText", () => {
  const text = (service: "calendar" | "mail" | "drive", kind: string, reason = "") =>
    connectorNoticeText({ service, kind, reason });

  it("종류마다 서비스 이름을 넣은 고정 문구를 쓴다", () => {
    expect(text("calendar", "service_disabled")).toContain(
      "Google 캘린더 API가 사용 설정되지 않아",
    );
    expect(text("mail", "quota_exceeded")).toBe(
      "Gmail 사용 한도를 넘었습니다. 잠시 뒤 다시 시도해 주세요.",
    );
    expect(text("mail", "rate_limited")).toContain("Gmail 요청이 많아");
    expect(text("mail", "permission_revoked")).toContain("다시 연결해 주세요");
    expect(text("calendar", "transient_error")).toContain("일시적으로 연결하지 못했습니다");
  });

  it("Drive의 문서 권한 부족은 권한을 추가하라고 안내한다", () => {
    expect(text("drive", "scope_missing", "scope_missing")).toBe(
      "문서 권한이 없습니다. 계정 연결 화면에서 문서 권한을 추가해 주세요.",
    );
    expect(text("drive", "rate_limited")).toContain("Google Drive 요청이 많아");
    expect(text("drive", "connector_error", "")).toBe("Google Drive에서 오류가 발생했습니다.");
  });

  it("문서 권한 안내는 상태 표시줄에 한 줄로 보인다", () => {
    render(
      <StatusBar
        items={connectorNoticeItems([
          { service: "drive", kind: "scope_missing", reason: "scope_missing" },
        ])}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "문서 권한이 없습니다. 계정 연결 화면에서 문서 권한을 추가해 주세요.",
    );
  });

  it("모르는 종류는 connector_error처럼 보이고 안전한 reason만 오류 코드로 덧붙인다", () => {
    expect(text("calendar", "weird", "backendError")).toBe(
      "Google 캘린더에서 오류가 발생했습니다. (오류 코드: backendError)",
    );
    expect(text("mail", "connector_error", "")).toBe("Gmail에서 오류가 발생했습니다.");
    const unsafe = text("mail", "connector_error", "<b>x</b> secret@example.test");
    expect(unsafe).toBe("Gmail에서 오류가 발생했습니다.");
    expect(text("mail", "connector_error", "a".repeat(41))).toBe("Gmail에서 오류가 발생했습니다.");
  });

  it("상태 표시줄에 서비스·종류별로 한 줄씩 보인다", () => {
    render(
      <StatusBar
        items={connectorNoticeItems([
          { service: "calendar", kind: "service_disabled", reason: "x" },
          { service: "mail", kind: "service_disabled", reason: "x" },
        ])}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Google 캘린더 API가");
    expect(screen.getByRole("status")).toHaveTextContent("Gmail API가");
  });
});

describe("connectionStatusItem", () => {
  it("재접속 중과 끊김을 구분하고 정상·종료 뒤에는 알리지 않는다", () => {
    expect(connectionStatusItem("connected", false)).toBeNull();
    expect(connectionStatusItem("reconnecting", false)?.text).toContain("다시 연결");
    expect(connectionStatusItem("closed", false)?.text).toContain("새로고침");
    expect(connectionStatusItem("closed", true)).toBeNull();
  });
});

describe("StatusBar", () => {
  it("오류는 alert, 나머지는 status 영역에 둔다", () => {
    render(
      <StatusBar
        items={[runStatusItem(run({ status: "running" })), connectionStatusItem("closed", false)]}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("작업 중입니다.");
    expect(screen.getByRole("alert")).toHaveTextContent("연결이 끊겼습니다");
  });

  it("표시할 것이 없어도 알림 영역은 남아 있다", () => {
    render(<StatusBar items={[null, null]} />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.getByRole("alert")).toBeEmptyDOMElement();
  });
});

describe("MessageList", () => {
  it("빈 대화는 처음 할 일을 안내한다", () => {
    render(
      <MessageList messages={[]} outgoing={null} sendStatus="idle" results={[]} working={false} />,
    );
    expect(screen.getByText(/물어보세요/)).toBeInTheDocument();
  });

  it("사용자·비서 메시지와 실행 결과를 같은 동사로 보여 준다", () => {
    render(
      <MessageList
        messages={[
          { id: "u1", role: "user", text: "메일 보내 줘" },
          { id: "a1", role: "assistant", text: "초안을 만들었습니다." },
        ]}
        outgoing={null}
        sendStatus="idle"
        results={[
          { approvalId: "ap", status: "succeeded", errorKind: null, toolName: "send_mail" },
        ]}
        working={false}
      />,
    );
    expect(screen.getByText("메일 보내 줘")).toBeInTheDocument();
    expect(screen.getByText("초안을 만들었습니다.")).toBeInTheDocument();
    expect(screen.getByText("메일을 보냈습니다.")).toBeInTheDocument();
  });

  it("전송 중·전송 실패인 메시지를 메시지 옆에 표시한다", () => {
    const { rerender } = render(
      <MessageList
        messages={[]}
        outgoing="보낼 말"
        sendStatus="sending"
        results={[]}
        working={false}
      />,
    );
    expect(screen.getByText("전송 중")).toBeInTheDocument();
    rerender(
      <MessageList
        messages={[]}
        outgoing="보낼 말"
        sendStatus="failed"
        results={[]}
        working={false}
      />,
    );
    expect(screen.getByText("전송하지 못했습니다")).toBeInTheDocument();
    expect(screen.getByText("보낼 말")).toBeInTheDocument();
  });

  it("작업 중에는 응답 자리를 미리 잡되 화면낭독기에는 읽히지 않는다", () => {
    const { container } = render(
      <MessageList messages={[]} outgoing={null} sendStatus="idle" results={[]} working />,
    );
    expect(container.querySelector(".message-working")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("isNearBottom", () => {
  it("맨 아래 근처면 따라가고 위로 올라갔으면 따라가지 않는다", () => {
    expect(isNearBottom({ scrollTop: 900, scrollHeight: 1000, clientHeight: 100 })).toBe(true);
    expect(isNearBottom({ scrollTop: 860, scrollHeight: 1000, clientHeight: 100 })).toBe(true);
    expect(isNearBottom({ scrollTop: 300, scrollHeight: 1000, clientHeight: 100 })).toBe(false);
  });
});

function Harness({ blocked, send }: { blocked: boolean; send: (text: string) => Promise<void> }) {
  const composer = useComposer(send);
  return (
    <Composer composer={composer} inputRef={createRef<HTMLTextAreaElement>()} blocked={blocked} />
  );
}

describe("Composer", () => {
  it("승인 대기 중에는 전송 버튼이 꺼지고 입력은 되며 Enter로도 전송되지 않는다", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    render(<Harness blocked send={send} />);
    const input = screen.getByLabelText("메시지");
    await userEvent.type(input, "나중에 보낼 말{Enter}");
    expect(input).toHaveValue("나중에 보낼 말");
    expect(screen.getByRole("button", { name: "전송" })).toBeDisabled();
    expect(send).not.toHaveBeenCalled();
    expect(input).toHaveAccessibleDescription(/승인 대기 중에는 전송할 수 없습니다/);
  });

  it("승인 대기가 아니면 전송 버튼 문구는 전송이고 Enter로 전송한다", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    render(<Harness blocked={false} send={send} />);
    await userEvent.type(screen.getByLabelText("메시지"), "안녕{Enter}");
    expect(send).toHaveBeenCalledWith("안녕");
    expect(screen.getByRole("button", { name: "전송" })).toBeEnabled();
  });
});
