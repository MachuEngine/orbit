import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApprovalPanel } from "./ApprovalPanel";
import { executionResultText, type Clock } from "./logic";

const clock: Clock = { now: () => new Date("2026-11-11T10:00:00+00:00") };

function approvalBody(overrides: Record<string, unknown> = {}) {
  return {
    id: "ap1",
    run_id: "r1",
    status: "pending",
    tool_name: "send_mail",
    args: { to: ["new@example.com"] },
    summary: { to_count: 1 },
    provenance: { "new@example.com": "external_content" },
    expires_at: "2026-11-11T10:05:00+00:00",
    decided_at: null,
    ...overrides,
  };
}

function stubFetch(approval: unknown) {
  const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) =>
    Promise.resolve(
      new Response(JSON.stringify(init?.method === "POST" ? { status: "approved" } : approval), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ApprovalPanel", () => {
  it("동작·출처 경고·남은 시간을 보여 주고 확인 버튼으로 결정을 보낸다", async () => {
    const fetchMock = stubFetch(approvalBody());
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByRole("heading", { name: "메일 보내기" })).toBeInTheDocument();
    expect(screen.getByText("new@example.com", { selector: "li" })).toBeInTheDocument();
    expect(screen.getByText("만료까지 5분")).toBeInTheDocument();
    expect(screen.getByText("보낸 뒤에는 되돌릴 수 없습니다.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    await waitFor(() => {
      expect(screen.getByText("결정을 전달했습니다.")).toBeInTheDocument();
    });
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit).method === "POST");
    expect((post?.[1] as RequestInit).body).toBe('{"decision":"approve"}');
  });

  it("pending이 아닌 승인은 버튼을 잠그고 상태를 알린다", async () => {
    stubFetch(approvalBody({ status: "rejected", decided_at: "2026-11-11T09:59:00+00:00" }));
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByText("실행하지 않기로 결정한 요청입니다.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "보내기" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "실행하지 않기" })).toBeDisabled();
  });
});

describe("executionResultText", () => {
  it("상태별로 다른 문구를 쓴다", () => {
    const texts = (["succeeded", "rejected", "expired", "failed", "needs_user"] as const).map(
      executionResultText,
    );
    expect(new Set(texts).size).toBe(5);
    expect(executionResultText("rejected")).toBe("실행하지 않았습니다.");
  });
});
