import { describe, expect, it } from "vitest";
import { MAX_SPEAK_CHARS, toSpeakableText, TRUNCATION_NOTICE } from "./speakable";

describe("toSpeakableText", () => {
  it("마크다운 기호를 빼고 글자만 남긴다", () => {
    const source = "# 오늘 일정\n\n- **10시** 팀 회의\n1. 점심 약속\n> 인용 문장\n`코드`도 읽는다";
    expect(toSpeakableText(source)).toBe(
      "오늘 일정 10시 팀 회의 점심 약속 인용 문장 코드도 읽는다",
    );
  });

  it("코드 블록은 통째로 뺀다(닫히지 않은 블록 포함)", () => {
    expect(toSpeakableText("앞 문장\n```ts\nconst a = 1;\n```\n뒤 문장")).toBe("앞 문장 뒤 문장");
    expect(toSpeakableText("앞 문장\n```\nconst secret = 1;")).toBe("앞 문장");
  });

  it("링크는 글자만 남기고 주소, 이메일, 이미지는 뺀다", () => {
    expect(toSpeakableText("[자료](https://example.com/a?b=1)를 보세요")).toBe("자료를 보세요");
    expect(toSpeakableText("주소는 https://example.com/x 와 www.example.org 입니다")).toBe(
      "주소는 와 입니다",
    );
    expect(toSpeakableText("보낸 사람은 alice@example.com 입니다")).toBe("보낸 사람은 입니다");
    expect(toSpeakableText("그림 ![로고](https://example.com/a.png) 끝")).toBe("그림 끝");
  });

  it("작업 집합 핸들을 빼고 남는 빈 괄호도 정리한다", () => {
    expect(toSpeakableText("회의(E1)와 메일 M3, M4 확인했습니다")).toBe("회의와 메일 확인했습니다");
    expect(toSpeakableText("문서 D2는 오래됐습니다")).toBe("문서 는 오래됐습니다");
    expect(toSpeakableText("근거 [M1, M2] 참고")).toBe("근거 참고");
  });

  it("핸들이 아닌 영문·숫자는 남긴다", () => {
    expect(toSpeakableText("MEET12 코드와 E 그리고 3시")).toBe("MEET12 코드와 E 그리고 3시");
  });

  it("600자 이하는 그대로 읽는다", () => {
    const text = "가".repeat(MAX_SPEAK_CHARS);
    expect(toSpeakableText(text)).toBe(text);
  });

  it("600자를 넘으면 앞부분만 읽고 안내 문구로 끝낸다", () => {
    const out = toSpeakableText("가".repeat(MAX_SPEAK_CHARS + 50));
    expect(out).toBe(`${"가".repeat(MAX_SPEAK_CHARS)}. ${TRUNCATION_NOTICE}`);
    const sentence = `${"가".repeat(MAX_SPEAK_CHARS - 1)}.` + "나".repeat(50);
    expect(toSpeakableText(sentence)).toBe(
      `${"가".repeat(MAX_SPEAK_CHARS - 1)}. ${TRUNCATION_NOTICE}`,
    );
  });

  it("뺄 것만 있으면 빈 문자열이다", () => {
    expect(toSpeakableText("```\ncode\n```")).toBe("");
    expect(toSpeakableText("https://example.com")).toBe("");
  });
});
