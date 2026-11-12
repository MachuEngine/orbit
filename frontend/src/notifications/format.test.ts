import { describe, expect, it } from "vitest";
import { clockText, formatUpcoming } from "./format";

// 시간대와 무관하게 로컬 시각 구성요소로 만든다.
const local = (day: number, hour: number, minute = 0) =>
  new Date(2026, 10, day, hour, minute).getTime();

describe("formatUpcoming", () => {
  const now = local(12, 10, 0);
  const iso = (ms: number) => new Date(ms).toISOString();

  it("한 시간 안이면 몇 분 뒤인지 쓴다", () => {
    expect(formatUpcoming(iso(now + 30_000), now)).toBe("곧");
    expect(formatUpcoming(iso(now + 5 * 60_000), now)).toBe("5분 뒤");
    expect(formatUpcoming(iso(now + 59 * 60_000), now)).toBe("59분 뒤");
  });

  it("같은 날·다음 날·그 뒤를 구분하고 시각을 붙인다", () => {
    expect(formatUpcoming(iso(local(12, 15, 0)), now)).toMatch(/^오늘 오후 3:00$/);
    expect(formatUpcoming(iso(local(13, 9, 30)), now)).toMatch(/^내일 오전 9:30$/);
    expect(formatUpcoming(iso(local(20, 9, 30)), now)).toMatch(/^11월 20일 오전 9:30$/);
  });

  it("해가 다르면 연도를 붙이고 읽을 수 없는 값은 빈 문자열이다", () => {
    expect(formatUpcoming(iso(new Date(2027, 0, 5, 9, 0).getTime()), now)).toMatch(
      /^2027년 1월 5일/,
    );
    expect(formatUpcoming("not-a-date", now)).toBe("");
    expect(clockText("not-a-date")).toBe("");
    expect(clockText(iso(local(12, 15, 5)))).toBe("오후 3:05");
  });
});
