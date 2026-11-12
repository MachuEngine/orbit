import { describe, expect, it } from "vitest";
import { conversationTitle, formatLastActivity } from "./format";

const now = new Date(2026, 10, 12, 12, 0, 0).getTime();
const ago = (ms: number) => new Date(now - ms).toISOString();

describe("formatLastActivity", () => {
  it("하루 안이면 상대 표현을 쓴다", () => {
    expect(formatLastActivity(ago(10_000), now)).toBe("방금 전");
    expect(formatLastActivity(ago(5 * 60_000), now)).toBe("5분 전");
    expect(formatLastActivity(ago(3 * 3_600_000), now)).toBe("3시간 전");
  });

  it("하루가 넘으면 날짜를 쓰고 해가 다르면 연도를 붙인다", () => {
    expect(formatLastActivity(new Date(2026, 10, 5, 9).toISOString(), now)).toBe("11월 5일");
    expect(formatLastActivity(new Date(2025, 11, 31, 9).toISOString(), now)).toBe(
      "2025년 12월 31일",
    );
  });

  it("읽을 수 없는 값은 빈 문자열이다", () => {
    expect(formatLastActivity("not-a-date", now)).toBe("");
  });
});

describe("conversationTitle", () => {
  it("제목이 없거나 공백이면 기본 문구를 쓴다", () => {
    expect(conversationTitle(null)).toBe("제목 없는 대화");
    expect(conversationTitle("  ")).toBe("제목 없는 대화");
    expect(conversationTitle("내일 일정")).toBe("내일 일정");
  });
});
