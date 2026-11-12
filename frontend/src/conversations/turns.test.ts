import { describe, expect, it } from "vitest";
import type { ConversationTurn } from "../api/schemas";
import { turnsToView } from "./turns";

const turn = (overrides: Partial<ConversationTurn>): ConversationTurn => ({
  run_id: "r1",
  status: "succeeded",
  user_text: "질문",
  assistant_text: "답",
  pending_approval_id: null,
  created_at: "2026-11-12T00:00:00Z",
  ...overrides,
});

describe("turnsToView", () => {
  it("끝난 턴은 사용자·비서 메시지를 순서대로 채우고 구독할 run이 없다", () => {
    const view = turnsToView([
      turn({ run_id: "r1" }),
      turn({ run_id: "r2", user_text: "또 질문", assistant_text: "또 답" }),
    ]);
    expect(view.messages.map((m) => [m.role, m.text])).toEqual([
      ["user", "질문"],
      ["assistant", "답"],
      ["user", "또 질문"],
      ["assistant", "또 답"],
    ]);
    expect(new Set(view.messages.map((m) => m.id)).size).toBe(4);
    expect(view.resumeRunId).toBeNull();
  });

  it("끝나지 않은 마지막 턴은 비서 응답을 넣지 않고 그 run을 이어서 구독한다", () => {
    const view = turnsToView([
      turn({ run_id: "r1" }),
      turn({ run_id: "r2", status: "running", assistant_text: "작성 중인 답" }),
    ]);
    expect(view.messages.map((m) => m.text)).toEqual(["질문", "답", "질문"]);
    expect(view.resumeRunId).toBe("r2");
  });

  it("비서 응답이 없는 성공 턴은 사용자 메시지만 남긴다", () => {
    const view = turnsToView([turn({ status: "succeeded", assistant_text: null })]);
    expect(view.messages).toHaveLength(1);
    expect(view.resumeRunId).toBeNull();
  });

  it("응답 없는 실패·취소 턴 뒤에 안내를 붙인다", () => {
    const failed = turnsToView([turn({ run_id: "f", status: "failed", assistant_text: null })]);
    expect(failed.messages.map((m) => [m.role, m.text])).toEqual([
      ["user", "질문"],
      ["notice", "이 요청은 처리하지 못했습니다."],
    ]);
    const cancelled = turnsToView([turn({ status: "cancelled", assistant_text: null })]);
    expect(cancelled.messages.at(-1)).toMatchObject({
      role: "notice",
      text: "이 요청은 취소되었습니다.",
    });
  });

  it("응답이 있으면 실패일 때만 안내를 덧붙인다", () => {
    const failed = turnsToView([turn({ status: "failed", assistant_text: "일부 답" })]);
    expect(failed.messages.map((m) => m.role)).toEqual(["user", "assistant", "notice"]);
    const cancelled = turnsToView([turn({ status: "cancelled", assistant_text: "일부 답" })]);
    expect(cancelled.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    const done = turnsToView([turn({})]);
    expect(done.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
  });

  it("끝나지 않은 마지막 턴에는 안내를 붙이지 않는다", () => {
    const view = turnsToView([turn({ status: "running", assistant_text: null })]);
    expect(view.messages.map((m) => m.role)).toEqual(["user"]);
  });

  it("턴이 없으면 비어 있다", () => {
    expect(turnsToView([])).toEqual({ messages: [], resumeRunId: null });
  });
});
