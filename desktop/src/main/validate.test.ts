import { describe, expect, it } from "vitest";
import { LIMITS } from "../shared/channels";
import { FALLBACK_TITLE, parseBadgeCount, parseNotifyPayload } from "./validate";

describe("parseNotifyPayload", () => {
  it("올바른 값을 돌려준다", () => {
    expect(parseNotifyPayload({ id: "n1", title: "합성 회의", body: "곧 시작합니다." })).toEqual({
      id: "n1",
      title: "합성 회의",
      body: "곧 시작합니다.",
    });
  });

  it("body가 없으면 빈 문자열이다", () => {
    expect(parseNotifyPayload({ id: "n1", title: "t" })).toEqual({
      id: "n1",
      title: "t",
      body: "",
    });
  });

  it("id가 상한이면 허용하고 넘으면 거부한다", () => {
    expect(parseNotifyPayload({ id: "a".repeat(LIMITS.idMax), title: "t" })).not.toBeNull();
    expect(parseNotifyPayload({ id: "a".repeat(LIMITS.idMax + 1), title: "t" })).toBeNull();
  });

  it.each([
    ["null", null],
    ["문자열", "x"],
    ["배열", []],
    ["id 없음", { title: "t" }],
    ["빈 id", { id: "", title: "t" }],
    ["숫자 id", { id: 1, title: "t" }],
    ["제어 문자가 든 id", { id: "a\u0000b", title: "t" }],
    ["모르는 키", { id: "n", title: "t", extra: 1 }],
  ])("거부한다: %s", (_name, value) => {
    expect(parseNotifyPayload(value)).toBeNull();
  });
});

describe("parseNotifyPayload 문구 정리", () => {
  it("제목이 상한을 넘으면 말줄여서 상한 안으로 자른다", () => {
    const parsed = parseNotifyPayload({ id: "n", title: "가".repeat(LIMITS.titleMax + 50) });
    expect(parsed?.title).toHaveLength(LIMITS.titleMax);
    expect(parsed?.title.endsWith("…")).toBe(true);
  });

  it("상한과 같은 길이는 그대로 둔다", () => {
    const title = "가".repeat(LIMITS.titleMax);
    expect(parseNotifyPayload({ id: "n", title })?.title).toBe(title);
  });

  it("서로게이트 쌍을 가운데서 끊지 않는다", () => {
    const parsed = parseNotifyPayload({ id: "n", title: "😀".repeat(LIMITS.titleMax) });
    expect(parsed?.title.length).toBeLessThanOrEqual(LIMITS.titleMax);
    expect(parsed?.title).not.toMatch(/[\ud800-\udbff]…$/);
  });

  it("본문이 상한을 넘으면 말줄인다", () => {
    const parsed = parseNotifyPayload({
      id: "n",
      title: "t",
      body: "b".repeat(LIMITS.bodyMax + 1),
    });
    expect(parsed?.body).toHaveLength(LIMITS.bodyMax);
    expect(parsed?.body.endsWith("…")).toBe(true);
  });

  it("제목이 비었거나 공백뿐이거나 문자열이 아니면 대체 문구를 쓴다", () => {
    for (const title of ["", "   ", "\u0000\n", 5, null, undefined]) {
      expect(parseNotifyPayload({ id: "n", title })?.title).toBe(FALLBACK_TITLE);
    }
  });

  it("제어 문자는 공백으로 바꾸고 앞뒤 공백을 지운다", () => {
    expect(parseNotifyPayload({ id: "n", title: " a\u0000b\nc\u0085d " })?.title).toBe("a b c d");
  });

  it("본문이 문자열이 아니면 빈 문자열이다", () => {
    expect(parseNotifyPayload({ id: "n", title: "t", body: {} })?.body).toBe("");
  });
});

describe("parseBadgeCount", () => {
  it("0 이상 상한 이하의 정수만 허용한다", () => {
    expect(parseBadgeCount(0)).toBe(0);
    expect(parseBadgeCount(7)).toBe(7);
    expect(parseBadgeCount(LIMITS.badgeMax)).toBe(LIMITS.badgeMax);
  });

  it.each([-1, 1.5, LIMITS.badgeMax + 1, Number.NaN, Infinity, "3", null, undefined, {}])(
    "거부한다: %s",
    (value) => {
      expect(parseBadgeCount(value)).toBeNull();
    },
  );
});
