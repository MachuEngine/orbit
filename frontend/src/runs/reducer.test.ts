import { describe, expect, it } from "vitest";
import { parseRunEvent, type RunEvent } from "./events";
import { initialRunViewState, runViewReducer, type RunViewState } from "./reducer";

function fold(events: RunEvent[], start: RunViewState = initialRunViewState): RunViewState {
  return events.reduce((state, event) => runViewReducer(state, { type: "event", event }), start);
}

const status = (seq: number, value: "running" | "waiting_approval" | "succeeded" | "failed") =>
  ({ seq, type: "status", payload: { status: value } }) satisfies RunEvent;

const approvalRequested = (seq: number, approvalId = "ap1") =>
  ({
    seq,
    type: "approval_requested",
    payload: { step: 1, approval_id: approvalId, tool_name: "send_mail", summary: { to_count: 1 } },
  }) satisfies RunEvent;

describe("runViewReducer", () => {
  it("이벤트 순서대로 메시지, 대기 중 승인, 실행 결과를 쌓는다", () => {
    const state = fold([
      status(1, "running"),
      { seq: 2, type: "assistant_text", payload: { step: 0, text: "메일을 쓰겠습니다." } },
      approvalRequested(3),
      status(4, "waiting_approval"),
    ]);
    expect(state.status).toBe("waiting_approval");
    expect(state.messages).toEqual([
      { id: "assistant-0-2", role: "assistant", text: "메일을 쓰겠습니다." },
    ]);
    expect(state.pendingApproval).toEqual({
      approvalId: "ap1",
      toolName: "send_mail",
      summary: { to_count: 1 },
    });

    const done = fold(
      [
        status(5, "running"),
        {
          seq: 6,
          type: "execution_result",
          payload: { step: 1, approval_id: "ap1", status: "succeeded" },
        },
        status(7, "succeeded"),
      ],
      state,
    );
    expect(done.pendingApproval).toBeNull();
    expect(done.results).toEqual([
      { approvalId: "ap1", status: "succeeded", errorKind: null, toolName: "send_mail" },
    ]);
    expect(done.finished).toBe(true);
    expect(done.lastSeq).toBe(7);
  });

  it("이미 반영한 seq는 무시한다(재접속 재생)", () => {
    const events: RunEvent[] = [
      { seq: 1, type: "assistant_text", payload: { step: 0, text: "하나" } },
      { seq: 2, type: "assistant_text", payload: { step: 1, text: "둘" } },
    ];
    const once = fold(events);
    const replayed = fold(events, once);
    expect(replayed).toBe(once);
    expect(replayed.messages).toHaveLength(2);
  });

  it("순서가 뒤바뀐 낮은 seq도 무시한다", () => {
    const state = fold([status(5, "running"), status(3, "failed")]);
    expect(state.status).toBe("running");
    expect(state.lastSeq).toBe(5);
  });

  it("종료 상태를 받으면 finished가 되고 대기 중 승인을 비운다", () => {
    const state = fold([approvalRequested(1), status(2, "waiting_approval"), status(3, "failed")]);
    expect(state.finished).toBe(true);
    expect(state.pendingApproval).toBeNull();
  });

  it("error 이벤트는 오류 종류를 기록한다", () => {
    const state = fold([
      { seq: 1, type: "error", payload: { error_kind: "llm_unavailable" } },
      status(2, "failed"),
    ]);
    expect(state.error).toBe("llm_unavailable");
    expect(state.status).toBe("failed");
  });

  it("다른 승인의 실행 결과는 대기 중 승인을 비우지 않는다", () => {
    const state = fold([
      approvalRequested(1, "ap2"),
      {
        seq: 2,
        type: "execution_result",
        payload: { step: 1, approval_id: "ap1", status: "failed", error_kind: "x" },
      },
    ]);
    expect(state.pendingApproval?.approvalId).toBe("ap2");
    expect(state.results[0]?.errorKind).toBe("x");
  });

  it("run_started는 메시지를 남기고 run 상태를 접수 직후(queued)로 되돌린다", () => {
    const withUser = runViewReducer(initialRunViewState, {
      type: "user_message",
      id: "user-1",
      text: "안녕",
    });
    const running = fold([status(1, "running")], withUser);
    const next = runViewReducer(running, { type: "run_started" });
    expect(next.messages).toHaveLength(1);
    expect(next.lastSeq).toBe(0);
    expect(next.status).toBe("queued");
  });

  it("형태가 다른 이벤트는 스트림 문제로 표시한다", () => {
    const state = runViewReducer(initialRunViewState, { type: "invalid_event" });
    expect(state.streamProblem).toBe("invalid_event");
  });
});

describe("execution_result 상태", () => {
  it.each(["rejected", "expired"] as const)("%s 결과를 받아들인다", (value) => {
    const body = JSON.stringify({ step: 1, approval_id: "a", status: value });
    const parsed = parseRunEvent("1", "execution_result", body);
    expect(parsed.kind).toBe("event");
    if (parsed.kind !== "event") return;
    expect(fold([parsed.event]).results[0]?.status).toBe(value);
  });
});

describe("parseRunEvent", () => {
  it("올바른 이벤트를 seq와 함께 해석한다", () => {
    expect(parseRunEvent("3", "status", '{"status":"running"}')).toEqual({
      kind: "event",
      event: { seq: 3, type: "status", payload: { status: "running" } },
    });
  });

  it("모르는 종류는 무시 대상으로, 알려진 종류의 잘못된 형태는 invalid로 구분한다", () => {
    expect(parseRunEvent("1", "future_thing", "{}")).toEqual({ kind: "unknown_type" });
    expect(parseRunEvent("1", "status", '{"status":"exploded"}')).toEqual({ kind: "invalid" });
    expect(parseRunEvent("1", "status", "not json")).toEqual({ kind: "invalid" });
    expect(parseRunEvent("", "status", '{"status":"running"}')).toEqual({ kind: "invalid" });
  });
});

describe("connector_notice", () => {
  const notice = (seq: number, service: "calendar" | "mail", kind: string, reason = "r") =>
    ({
      seq,
      type: "connector_notice",
      payload: { step: 1, service, kind, reason },
    }) satisfies RunEvent;

  it("이벤트 형태를 검증하고 같은 service+kind는 한 번만 담는다", () => {
    const parsed = parseRunEvent(
      "1",
      "connector_notice",
      '{"step":1,"service":"calendar","kind":"service_disabled","reason":"accessNotConfigured"}',
    );
    expect(parsed.kind).toBe("event");
    expect(
      parseRunEvent("1", "connector_notice", '{"step":1,"service":"drive","kind":"x","reason":""}')
        .kind,
    ).toBe("invalid");

    const state = fold([
      notice(1, "calendar", "service_disabled"),
      notice(2, "calendar", "service_disabled"),
      notice(3, "mail", "service_disabled"),
      notice(4, "calendar", "rate_limited"),
    ]);
    expect(state.connectorNotices.map((n) => `${n.service}:${n.kind}`)).toEqual([
      "calendar:service_disabled",
      "mail:service_disabled",
      "calendar:rate_limited",
    ]);
  });

  it("새 run을 시작하면 비운다", () => {
    const state = fold([notice(1, "mail", "quota_exceeded")]);
    expect(runViewReducer(state, { type: "run_started" }).connectorNotices).toEqual([]);
  });
});

const assistantText = (seq: number, text: string) =>
  ({ seq, type: "assistant_text", payload: { step: 0, text } }) satisfies RunEvent;

describe("대화 바꾸기", () => {
  it("메시지를 바꾸고 이전 run의 상태를 모두 비운다", () => {
    const before = fold([
      status(1, "running"),
      approvalRequested(2),
      status(3, "waiting_approval"),
    ]);
    const messages = [{ id: "m1", role: "user" as const, text: "이전 대화" }];
    const after = runViewReducer(before, { type: "replace_messages", messages });
    expect(after).toEqual({ ...initialRunViewState, messages });
  });

  it("같은 대화의 두 번째 run에서도 비서 메시지 key가 겹치지 않는다", () => {
    const first = fold([assistantText(1, "첫 답")]);
    const second = fold(
      [assistantText(1, "둘째 답")],
      runViewReducer(first, { type: "run_started" }),
    );
    const ids = second.messages.map((m) => m.id);
    expect(new Set(ids).size).toBe(2);
  });
});
