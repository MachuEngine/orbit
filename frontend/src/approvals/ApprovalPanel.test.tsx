import { render, screen, waitFor, within } from "@testing-library/react";
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
    args: {
      to: ["kim@example.com", "new@example.com"],
      subject: "합성 제목",
      body: "합성 본문",
    },
    summary: { to_count: 2 },
    provenance: { "kim@example.com": "user", "new@example.com": "external_content" },
    expires_at: "2026-11-11T10:05:00+00:00",
    decided_at: null,
    ...overrides,
  };
}

function stubFetch(approval: unknown, decisionStatus = 200) {
  const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) =>
    Promise.resolve(
      init?.method === "POST"
        ? new Response(JSON.stringify(decisionStatus === 200 ? { status: "approved" } : {}), {
            status: decisionStatus,
            headers: { "Content-Type": "application/json" },
          })
        : new Response(JSON.stringify(approval), {
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
  it("동작 제목·수신자·제목·본문·남은 시간을 사람이 읽는 형태로 보여 준다", async () => {
    stubFetch(approvalBody());
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByRole("heading", { name: "메일 보내기" })).toBeInTheDocument();
    expect(screen.getByText("kim@example.com")).toBeInTheDocument();
    expect(screen.getByText("합성 제목")).toBeInTheDocument();
    expect(screen.getByText("합성 본문")).toBeInTheDocument();
    expect(screen.getByText("남은 시간 5분")).toBeInTheDocument();
    // JSON 원문을 보여 주지 않는다.
    expect(document.querySelector("pre")).toBeNull();
    expect(screen.queryByText(/"to"/)).toBeNull();
  });

  it("외부 출처 주소는 경고 표시와 이유를 보이고 직접 입력한 주소는 경고하지 않는다", async () => {
    stubFetch(approvalBody());
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    const flagged = (await screen.findByText("new@example.com")).closest("li");
    expect(flagged).not.toBeNull();
    expect(within(flagged as HTMLElement).getByText("외부 출처")).toBeInTheDocument();
    expect(
      within(flagged as HTMLElement).getByText(/메일·문서 본문에서 가져온 주소/),
    ).toBeVisible();
    const clean = screen.getByText("kim@example.com").closest("li");
    expect(within(clean as HTMLElement).queryByText("외부 출처")).toBeNull();
    expect(screen.getByText(/확인이 필요한 값이 들어 있습니다/)).toBeInTheDocument();
  });

  it("모델이 만든 주소는 비서가 만든 값이라고 표시한다", async () => {
    stubFetch(approvalBody({ provenance: { "new@example.com": "model" } }));
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByText("비서가 만든 값")).toBeInTheDocument();
  });

  it("출처 정보가 없는 주소는 확인하지 못했다고 경고한다(fail-closed)", async () => {
    stubFetch(approvalBody({ provenance: { "kim@example.com": "user" } }));
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    const unknown = (await screen.findByText("new@example.com")).closest("li");
    expect(within(unknown as HTMLElement).getByText("출처 확인 안 됨")).toBeInTheDocument();
    expect(
      within(unknown as HTMLElement).getByText(/출처를 확인하지 못한 주소입니다/),
    ).toBeVisible();
    const known = screen.getByText("kim@example.com").closest("li");
    expect(within(known as HTMLElement).queryByText("출처 확인 안 됨")).toBeNull();
  });

  it("화면이 보여 주지 않는 인자가 있으면 표시되지 않은 항목이 있다고 경고한다", async () => {
    stubFetch(
      approvalBody({
        args: { to: ["kim@example.com"], subject: "제목", body: "본문", bcc: ["x@example.com"] },
        provenance: { "kim@example.com": "user" },
      }),
    );
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByText(/표시되지 않은 항목이 있습니다\(bcc\)/)).toBeInTheDocument();
  });

  it("모든 인자를 보여 줄 수 있으면 숨겨진 항목 경고가 없다", async () => {
    stubFetch(
      approvalBody({ provenance: { "kim@example.com": "user", "new@example.com": "user" } }),
    );
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    await screen.findByRole("heading", { name: "메일 보내기" });
    expect(screen.queryByText(/표시되지 않은 항목/)).toBeNull();
  });

  it("일정 변경은 서버가 준 대상 제목과 변경 전·후 시간을 보여 준다", async () => {
    stubFetch(
      approvalBody({
        tool_name: "update_event_time",
        args: {
          calendar_id: "c",
          event_id: "provider-event-id",
          start: "2026-11-12T05:00:00Z",
          end: "2026-11-12T06:00:00Z",
        },
        summary: { handle: "E2" },
        provenance: {},
        target: {
          kind: "event",
          title: "점심",
          start: "2026-11-12T03:00:00Z",
          end: "2026-11-12T04:00:00Z",
        },
      }),
    );
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByText("점심")).toBeInTheDocument();
    expect(screen.getByText("변경 전")).toBeInTheDocument();
    expect(screen.getByText("변경 후")).toBeInTheDocument();
    expect(screen.queryByText(/provider-event-id/)).toBeNull();
    expect(screen.queryByText(/표시되지 않은 항목/)).toBeNull();
  });

  it("네트워크 오류는 결정이 전달됐는지 알 수 없다고 알리고 다시 누를 수 있다", async () => {
    const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) =>
      init?.method === "POST"
        ? Promise.reject(new TypeError("network"))
        : Promise.resolve(
            new Response(JSON.stringify(approvalBody()), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    await userEvent.click(await screen.findByRole("button", { name: "메일 보내기" }));
    expect(
      await screen.findByText(
        "결정이 전달됐는지 확인하지 못했습니다. 다시 눌러도 두 번 실행되지 않습니다.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "메일 보내기" })).toBeEnabled();
  });

  it("인자 목록에 없는데 경고가 붙은 주소도 빠뜨리지 않는다", async () => {
    stubFetch(approvalBody({ provenance: { "ghost@example.com": "external_content" } }));
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByText("ghost@example.com")).toBeInTheDocument();
    expect(screen.getByText("확인이 필요한 주소")).toBeInTheDocument();
  });

  it("메일 발송은 되돌릴 수 없다고 표시하고 일정 변경은 표시하지 않는다", async () => {
    stubFetch(approvalBody());
    const { unmount } = render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByText("되돌릴 수 없음")).toBeInTheDocument();
    unmount();

    stubFetch(
      approvalBody({
        tool_name: "update_event_time",
        args: {
          calendar_id: "cal-1",
          event_id: "provider-event-id",
          start: "2026-11-12T05:30:00+00:00",
          end: "2026-11-12T06:00:00+00:00",
        },
        summary: { handle: "E1" },
        provenance: {},
      }),
    );
    render(<ApprovalPanel approvalId="ap2" clock={clock} />);
    expect(await screen.findByRole("heading", { name: "일정 시간 변경" })).toBeInTheDocument();
    expect(screen.queryByText("되돌릴 수 없음")).toBeNull();
    expect(screen.getByText("일정 E1")).toBeInTheDocument();
    // 제공자 ID는 화면에 나오지 않는다.
    expect(screen.queryByText(/provider-event-id/)).toBeNull();
    expect(screen.getByRole("button", { name: "일정 시간 변경" })).toBeInTheDocument();
  });

  it("긴 본문은 접어 두고 펼쳐 볼 수 있다", async () => {
    const body = "긴 본문입니다. ".repeat(40);
    stubFetch(approvalBody({ args: { to: ["kim@example.com"], subject: "제목", body } }));
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    const summary = await screen.findByText("전체 본문 보기");
    const details = summary.closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    await userEvent.click(summary);
    expect(details).toHaveAttribute("open");
  });

  it("확인 버튼은 동작 동사이고 결정만 서버에 보낸다. 결과를 단정하지 않는다", async () => {
    const fetchMock = stubFetch(approvalBody());
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    await userEvent.click(await screen.findByRole("button", { name: "메일 보내기" }));
    await waitFor(() => {
      expect(screen.getByText(/결정을 전달했습니다/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/보냈습니다/)).toBeNull();
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit).method === "POST");
    expect((post?.[1] as RequestInit).body).toBe('{"decision":"approve"}');
    // 전달한 뒤에는 다시 누를 수 없다.
    expect(screen.getByRole("button", { name: "메일 보내기" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "실행하지 않기" })).toBeDisabled();
  });

  it("실행하지 않기는 reject를 보낸다", async () => {
    const fetchMock = stubFetch(approvalBody());
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    await userEvent.click(await screen.findByRole("button", { name: "실행하지 않기" }));
    await waitFor(() => {
      expect(screen.getByText(/실행하지 않습니다/)).toBeInTheDocument();
    });
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit).method === "POST");
    expect((post?.[1] as RequestInit).body).toBe('{"decision":"reject"}');
  });

  it("pending이 아닌 승인은 버튼을 잠그고 상태를 알린다", async () => {
    stubFetch(approvalBody({ status: "rejected", decided_at: "2026-11-11T09:59:00+00:00" }));
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByText("실행하지 않기로 결정한 요청입니다.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "메일 보내기" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "실행하지 않기" })).toBeDisabled();
  });

  it("만료된 승인은 버튼을 잠근다", async () => {
    stubFetch(approvalBody({ expires_at: "2026-11-11T09:59:00+00:00" }));
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    expect(await screen.findByText("만료되었습니다.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "메일 보내기" })).toBeDisabled();
  });

  it("이미 결정된 요청(409)이면 알리고 버튼을 잠근다", async () => {
    stubFetch(approvalBody(), 409);
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    await userEvent.click(await screen.findByRole("button", { name: "메일 보내기" }));
    expect(await screen.findByText("이미 결정된 요청입니다.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "메일 보내기" })).toBeDisabled();
  });

  it("카드가 나타나면 카드로 포커스가 가고 버튼에는 가지 않는다", async () => {
    stubFetch(approvalBody());
    render(<ApprovalPanel approvalId="ap1" clock={clock} />);
    await screen.findByRole("heading", { name: "메일 보내기" });
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "메일 보내기" })).toHaveFocus();
    });
  });
});

describe("executionResultText", () => {
  it("상태별로 다른 문구를 쓴다", () => {
    const texts = (["succeeded", "rejected", "expired", "failed", "needs_user"] as const).map(
      (status) => executionResultText(status),
    );
    expect(new Set(texts).size).toBe(5);
    expect(executionResultText("rejected")).toBe("실행하지 않았습니다.");
  });

  it("동작을 알면 버튼과 같은 동사로 결과를 알린다", () => {
    expect(executionResultText("succeeded", "send_mail")).toBe("메일을 보냈습니다.");
    expect(executionResultText("failed", "send_mail")).toBe("메일을 보내지 못했습니다.");
    expect(executionResultText("succeeded", "cancel_event")).toBe("일정을 취소했습니다.");
  });
});
