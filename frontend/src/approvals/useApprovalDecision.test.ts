import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import { useApprovalDecision, type DecideFn } from "./useApprovalDecision";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("useApprovalDecision", () => {
  it("전송 중 중복 클릭은 한 번만 보낸다", async () => {
    const pending = deferred<{ status: "approved" }>();
    const decide = vi.fn().mockReturnValue(pending.promise);
    const { result } = renderHook(() => useApprovalDecision("ap1", decide as unknown as DecideFn));

    let first!: Promise<void>;
    act(() => {
      first = result.current.submit("approve");
      void result.current.submit("approve");
      void result.current.submit("reject");
    });
    expect(decide).toHaveBeenCalledTimes(1);
    expect(result.current.state.phase).toBe("submitting");

    await act(async () => {
      pending.resolve({ status: "approved" });
      await first;
    });
    expect(result.current.state).toEqual({ phase: "done", decision: "approve" });

    await act(async () => {
      await result.current.submit("reject");
    });
    expect(decide).toHaveBeenCalledTimes(1);
  });

  it("409 expired는 만료로, 그 밖의 409는 이미 결정됨으로 알리고 다시 보내지 않는다", async () => {
    const decide = vi
      .fn()
      .mockRejectedValue(new ApiError("conflict", { status: 409, detail: "expired" }));
    const { result } = renderHook(() => useApprovalDecision("ap1", decide as unknown as DecideFn));
    await act(async () => {
      await result.current.submit("approve");
    });
    expect(result.current.state).toEqual({ phase: "conflict", reason: "expired" });
    await act(async () => {
      await result.current.submit("approve");
    });
    expect(decide).toHaveBeenCalledTimes(1);

    const other = vi
      .fn()
      .mockRejectedValue(new ApiError("conflict", { status: 409, detail: "이미" }));
    const second = renderHook(() => useApprovalDecision("ap2", other as unknown as DecideFn));
    await act(async () => {
      await second.result.current.submit("reject");
    });
    expect(second.result.current.state).toEqual({ phase: "conflict", reason: "already_decided" });
  });

  it("네트워크 오류는 다시 시도할 수 있다", async () => {
    const decide = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("network", {}))
      .mockResolvedValueOnce({ status: "approved" });
    const { result } = renderHook(() => useApprovalDecision("ap1", decide as unknown as DecideFn));
    await act(async () => {
      await result.current.submit("approve");
    });
    expect(result.current.state).toEqual({ phase: "error", kind: "network" });
    await act(async () => {
      await result.current.submit("approve");
    });
    expect(result.current.state).toEqual({ phase: "done", decision: "approve" });
    expect(decide).toHaveBeenCalledTimes(2);
  });
});
