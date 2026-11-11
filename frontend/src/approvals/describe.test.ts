import { describe, expect, it } from "vitest";
import { describeApproval, formatTimeRange, hasProvenanceWarning } from "./describe";

describe("describeApproval", () => {
  it("메일은 수신자·제목·본문 항목으로 나눈다", () => {
    const view = describeApproval({
      tool_name: "send_mail",
      args: { to: ["A@Example.com"], cc: ["b@example.com"], subject: "제목", body: "본문" },
      summary: {},
      provenance: { "a@example.com": "user", "b@example.com": "model" },
    });
    expect(view.rows.map((row) => row.label)).toEqual(["받는 사람", "참조", "제목", "본문"]);
    const to = view.rows[0];
    expect(to?.kind === "people" && to.people[0]).toMatchObject({
      email: "A@Example.com",
      source: "user",
      flagged: false,
    });
    const cc = view.rows[1];
    expect(cc?.kind === "people" && cc.people[0]?.flagged).toBe(true);
    expect(hasProvenanceWarning(view)).toBe(true);
  });

  it("출처가 user·workspace_data뿐이면 경고가 없다", () => {
    const view = describeApproval({
      tool_name: "send_mail",
      args: { to: ["a@example.com"], subject: "s", body: "b" },
      summary: {},
      provenance: { "a@example.com": "workspace_data" },
    });
    expect(hasProvenanceWarning(view)).toBe(false);
  });

  it("본문이 짧으면 접지 않고 길면 접는다", () => {
    const short = describeApproval({
      tool_name: "send_mail",
      args: { to: ["a@example.com"], subject: "s", body: "짧은 본문" },
      summary: {},
      provenance: {},
    });
    const longRow = describeApproval({
      tool_name: "send_mail",
      args: { to: ["a@example.com"], subject: "s", body: "가".repeat(300) },
      summary: {},
      provenance: {},
    }).rows.find((row) => row.kind === "longtext");
    expect(short.rows.find((row) => row.kind === "longtext")).toMatchObject({ collapsible: false });
    expect(longRow).toMatchObject({ collapsible: true });
    expect(longRow?.kind === "longtext" && longRow.excerpt.endsWith("…")).toBe(true);
  });

  it("일정은 제공자 ID 없이 핸들과 시간만 보여 준다", () => {
    const view = describeApproval({
      tool_name: "update_event_time",
      args: {
        calendar_id: "c",
        event_id: "secret-id",
        start: "2026-11-12T05:30:00Z",
        end: "2026-11-12T06:00:00Z",
      },
      summary: { handle: "E2" },
      provenance: {},
    });
    expect(JSON.stringify(view)).not.toContain("secret-id");
    expect(view.rows[0]).toEqual({ kind: "text", label: "대상 일정", value: "일정 E2" });
  });

  it("모르는 동작은 JSON이 아니라 이름과 값 목록으로 보여 준다", () => {
    const view = describeApproval({
      tool_name: "future_tool",
      args: { name: "값", count: 3, nested: { a: 1 } },
      summary: {},
      provenance: {},
    });
    expect(view.rows).toEqual([
      { kind: "text", label: "name", value: "값" },
      { kind: "text", label: "count", value: "3" },
    ]);
  });
});

describe("출처 fail-closed와 숨겨진 항목", () => {
  it("출처가 없는 수신자·참석자는 경고 대상이다", () => {
    const mail = describeApproval({
      tool_name: "send_mail",
      args: { to: ["a@example.com"], subject: "s", body: "b" },
      summary: {},
      provenance: {},
    });
    expect(hasProvenanceWarning(mail)).toBe(true);
    const event = describeApproval({
      tool_name: "create_event",
      args: {
        title: "t",
        start: "2026-11-12T05:00:00Z",
        end: "2026-11-12T06:00:00Z",
        attendees: ["a@example.com"],
      },
      summary: {},
      provenance: {},
    });
    expect(hasProvenanceWarning(event)).toBe(true);
  });

  it("알려진 도구의 모르는 인자 이름을 숨겨진 항목으로 모은다(내부 ID는 제외)", () => {
    const view = describeApproval({
      tool_name: "cancel_event",
      args: { kind: "cancel_event", calendar_id: "c", event_id: "e", reason: "x" },
      summary: {},
      provenance: {},
    });
    expect(view.hiddenKeys).toEqual(["reason"]);
    const clean = describeApproval({
      tool_name: "cancel_event",
      args: { kind: "cancel_event", calendar_id: "c", event_id: "e" },
      summary: {},
      provenance: {},
    });
    expect(clean.hiddenKeys).toEqual([]);
  });

  it("모르는 도구의 객체 인자는 숨겨진 항목이다", () => {
    const view = describeApproval({
      tool_name: "future_tool",
      args: { nested: { a: 1 } },
      summary: {},
      provenance: {},
    });
    expect(view.hiddenKeys).toEqual(["nested"]);
  });

  it("메일 답장은 대상 스레드 제목을 보여 준다", () => {
    const view = describeApproval({
      tool_name: "send_mail",
      args: { to: ["a@example.com"], subject: "s", body: "b", reply_to_thread_id: "t" },
      summary: {},
      provenance: { "a@example.com": "user" },
      target: { kind: "thread", subject: "프로젝트 일정 문의" },
    });
    expect(view.rows).toContainEqual({
      kind: "text",
      label: "답장할 대화",
      value: "프로젝트 일정 문의",
    });
  });
});

describe("formatTimeRange", () => {
  it("같은 날이면 끝 시각만 쓰고 시간대를 덧붙인다", () => {
    const text = formatTimeRange("2026-11-12T05:30:00Z", "2026-11-12T06:00:00Z", "Asia/Seoul");
    expect(text).toContain("14:30");
    expect(text).toContain("– 15:00");
    expect(text).toMatch(/\(GMT\+9\)$/);
  });

  it("해석할 수 없는 값은 받은 그대로 쓴다", () => {
    expect(formatTimeRange("가", "나")).toBe("가 – 나");
  });
});
