import { describe, expect, it } from "vitest";
import { NotificationDedup } from "./dedup";

describe("NotificationDedup", () => {
  it("같은 ID는 한 번만 띄운다", () => {
    const dedup = new NotificationDedup();
    expect(dedup.shouldShow("n1")).toBe(true);
    expect(dedup.shouldShow("n1")).toBe(false);
    expect(dedup.shouldShow("n2")).toBe(true);
    expect(dedup.shouldShow("n2")).toBe(false);
  });

  it("용량을 넘으면 가장 오래된 ID부터 잊는다", () => {
    const dedup = new NotificationDedup(2);
    dedup.shouldShow("a");
    dedup.shouldShow("b");
    dedup.shouldShow("c"); // a를 잊는다
    expect(dedup.shouldShow("b")).toBe(false);
    expect(dedup.shouldShow("c")).toBe(false);
    expect(dedup.shouldShow("a")).toBe(true);
  });

  it("용량이 1 미만이면 거부한다", () => {
    expect(() => new NotificationDedup(0)).toThrow(RangeError);
  });
});
