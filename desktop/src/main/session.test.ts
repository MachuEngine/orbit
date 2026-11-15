import { describe, expect, it, vi } from "vitest";
import { clearSessionData } from "./session";

describe("clearSessionData", () => {
  it("저장소와 캐시를 모두 지운다", async () => {
    const ses = {
      clearStorageData: vi.fn().mockResolvedValue(undefined),
      clearCache: vi.fn().mockResolvedValue(undefined),
    };
    expect(await clearSessionData(ses)).toBe(true);
    expect(ses.clearStorageData).toHaveBeenCalledTimes(1);
    expect(ses.clearStorageData).toHaveBeenCalledWith();
    expect(ses.clearCache).toHaveBeenCalledTimes(1);
  });

  it("하나가 실패해도 나머지를 시도하고 false를 돌려준다", async () => {
    const ses = {
      clearStorageData: vi.fn().mockRejectedValue(new Error("fail")),
      clearCache: vi.fn().mockResolvedValue(undefined),
    };
    expect(await clearSessionData(ses)).toBe(false);
    expect(ses.clearCache).toHaveBeenCalledTimes(1);
  });
});
