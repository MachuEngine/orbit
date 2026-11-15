import { describe, expect, it } from "vitest";
import { DEFAULT_SERVER_URL, DEFAULT_SHORTCUT, parseServerOrigin, parseShortcut } from "./config";

describe("parseServerOrigin", () => {
  it("값이 없으면 기본 주소를 쓴다", () => {
    expect(parseServerOrigin(undefined)).toBe(new URL(DEFAULT_SERVER_URL).origin);
    expect(parseServerOrigin("  ")).toBe(new URL(DEFAULT_SERVER_URL).origin);
  });

  it("출처만 있는 http/https 주소를 받는다", () => {
    expect(parseServerOrigin("http://localhost:5174")).toBe("http://localhost:5174");
    expect(parseServerOrigin("https://orbit.example.test/")).toBe("https://orbit.example.test");
  });

  it("http는 loopback에서만 허용한다", () => {
    expect(parseServerOrigin("http://localhost:5173")).toBe("http://localhost:5173");
    expect(parseServerOrigin("http://127.0.0.1:8000")).toBe("http://127.0.0.1:8000");
    expect(parseServerOrigin("http://[::1]:5173")).toBe("http://[::1]:5173");
    expect(parseServerOrigin("https://orbit.example.test")).toBe("https://orbit.example.test");
  });

  it.each([
    "http://orbit.example.test",
    "http://192.168.0.10:5173",
    "http://0.0.0.0:5173",
    "http://localhost.evil.com",
    "http://127.0.0.1.evil.com",
  ])("loopback이 아닌 http는 거부한다: %s", (value) => {
    expect(() => parseServerOrigin(value)).toThrow(/https/);
  });

  it.each([
    "ftp://localhost",
    "file:///tmp",
    "javascript:alert(1)",
    "localhost:5173",
    "http://user:pw@localhost:5173",
    "http://localhost:5173/app",
    "http://localhost:5173/?a=1",
    "http://localhost:5173/#x",
    "not a url",
  ])("거부한다: %s", (value) => {
    expect(() => parseServerOrigin(value)).toThrow();
  });
});

describe("parseShortcut", () => {
  it("기본값과 지정값, 끄기를 구분한다", () => {
    expect(parseShortcut(undefined)).toBe(DEFAULT_SHORTCUT);
    expect(parseShortcut(" CommandOrControl+Shift+O ")).toBe("CommandOrControl+Shift+O");
    expect(parseShortcut("")).toBeNull();
    expect(parseShortcut("OFF")).toBeNull();
  });
});
