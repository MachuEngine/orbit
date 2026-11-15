import { describe, expect, it } from "vitest";
import { decideNavigation, decideWindowOpen, isGoogleAuthUrl, isServerUrl } from "./navigation";

const ORIGIN = "http://localhost:5173";

describe("isServerUrl", () => {
  it.each([
    "http://localhost:5173",
    "http://localhost:5173/",
    "http://localhost:5173/chat?x=1#top",
    "http://LOCALHOST:5173/path",
  ])("서버 출처는 허용한다: %s", (url) => {
    expect(isServerUrl(url, ORIGIN)).toBe(true);
  });

  it.each([
    ["호스트 뒤에 도메인을 붙인 우회", "http://localhost:5173.evil.com/"],
    ["접두사만 같은 호스트", "http://localhost.evil.com:5173/"],
    ["사용자 정보로 호스트를 가린 우회", "http://localhost:5173@evil.com/"],
    ["사용자 정보가 붙은 서버 주소", "http://user:pw@localhost:5173/"],
    ["아이디만 붙은 서버 주소", "http://user@localhost:5173/"],
    ["다른 포트", "http://localhost:5174/"],
    ["포트 없음(80)", "http://localhost/"],
    ["https로 바꾼 같은 호스트", "https://localhost:5173/"],
    ["file 스킴", "file:///etc/passwd"],
    ["javascript 스킴", "javascript:alert(1)"],
    ["data 스킴", "data:text/html,<h1>x</h1>"],
    ["blob 스킴", "blob:http://localhost:5173/abc"],
    ["about 스킴", "about:blank"],
    ["스킴 없는 값", "localhost:5173/"],
    ["빈 문자열", ""],
    ["백슬래시로 가린 우회", "http://evil.com\\@localhost:5173/"],
    ["IP 표기 우회", "http://127.0.0.1:5173/"],
  ])("%s는 거부한다", (_name, url) => {
    expect(isServerUrl(url, ORIGIN)).toBe(false);
  });

  it("https 서버 출처도 같은 규칙을 쓴다", () => {
    expect(isServerUrl("https://orbit.example.test/a", "https://orbit.example.test")).toBe(true);
    expect(isServerUrl("http://orbit.example.test/a", "https://orbit.example.test")).toBe(false);
    expect(isServerUrl("https://orbit.example.test:8443/a", "https://orbit.example.test")).toBe(
      false,
    );
  });
});

describe("isGoogleAuthUrl", () => {
  it("Google 로그인 주소만 허용한다", () => {
    expect(isGoogleAuthUrl("https://accounts.google.com/o/oauth2/v2/auth?client_id=x")).toBe(true);
  });

  it.each([
    "http://accounts.google.com/",
    "https://accounts.google.com.evil.com/",
    "https://evilaccounts.google.com/",
    "https://accounts.google.com@evil.com/",
    "https://user:pw@accounts.google.com/",
    "https://accounts.google.com:8443/",
    "https://www.google.com/",
    "https://google.com/",
    "javascript://accounts.google.com/%0Aalert(1)",
    "file://accounts.google.com/",
    "",
  ])("그 밖의 주소는 거부한다: %s", (url) => {
    expect(isGoogleAuthUrl(url)).toBe(false);
  });
});

describe("decideNavigation", () => {
  it("서버 출처는 허용한다", () => {
    expect(decideNavigation("http://localhost:5173/chat", ORIGIN)).toEqual({ action: "allow" });
  });

  it("Google 로그인 주소는 시스템 브라우저로 넘긴다", () => {
    const url = "https://accounts.google.com/signin";
    expect(decideNavigation(url, ORIGIN)).toEqual({ action: "external", url });
  });

  it.each([
    "https://example.com/",
    "http://localhost:5173.evil.com/",
    "http://localhost:5173@evil.com/",
    "file:///tmp/x.html",
    "javascript:alert(1)",
  ])("그 밖의 주소는 막는다: %s", (url) => {
    expect(decideNavigation(url, ORIGIN)).toEqual({ action: "block" });
  });
});

describe("시스템 브라우저로 넘기는 값", () => {
  it("원본 문자열이 아니라 정규화된 href를 넘긴다", () => {
    expect(decideNavigation("https://ACCOUNTS.google.com", ORIGIN)).toEqual({
      action: "external",
      url: "https://accounts.google.com/",
    });
    expect(decideWindowOpen("https://accounts.google.com/a b\t")).toEqual({
      external: "https://accounts.google.com/a%20b",
    });
  });
});

describe("decideWindowOpen", () => {
  it("Google 주소만 시스템 브라우저로 연다", () => {
    expect(decideWindowOpen("https://accounts.google.com/x")).toEqual({
      external: "https://accounts.google.com/x",
    });
  });

  it("서버 출처 주소도 새 창으로는 열지 않는다", () => {
    expect(decideWindowOpen("http://localhost:5173/")).toEqual({ external: null });
    expect(decideWindowOpen("https://example.com/")).toEqual({ external: null });
    expect(decideWindowOpen("javascript:alert(1)")).toEqual({ external: null });
  });
});
