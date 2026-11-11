import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { initialRunViewState } from "../runs/reducer";
import { MessageList } from "./MessageList";
import { StatusBar } from "./StatusBar";
import { connectionStatusItem, runStatusItem } from "./status";
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
    expect(screen.getByText("보내는 중")).toBeInTheDocument();
    rerender(
      <MessageList
        messages={[]}
        outgoing="보낼 말"
        sendStatus="failed"
        results={[]}
        working={false}
      />,
    );
    expect(screen.getByText("보내지 못했습니다")).toBeInTheDocument();
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
