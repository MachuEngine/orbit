import { describe, expect, it } from "vitest";
import { isPermissionAllowed, micAccessAction } from "./permissions";

const ORIGIN = "http://localhost:5173";
const FROM = "http://localhost:5173/";

describe("isPermissionAllowed", () => {
  it("서버 출처의 오디오 media를 허용한다", () => {
    expect(
      isPermissionAllowed(
        { permission: "media", requestingUrl: FROM, mediaTypes: ["audio"] },
        ORIGIN,
      ),
    ).toBe(true);
  });

  it("비디오가 들어 있으면 거부한다", () => {
    for (const mediaTypes of [["video"], ["audio", "video"]]) {
      expect(
        isPermissionAllowed({ permission: "media", requestingUrl: FROM, mediaTypes }, ORIGIN),
      ).toBe(false);
    }
  });

  it("종류를 알 수 없는 media는 거부한다", () => {
    expect(isPermissionAllowed({ permission: "media", requestingUrl: FROM }, ORIGIN)).toBe(false);
    expect(
      isPermissionAllowed({ permission: "media", requestingUrl: FROM, mediaTypes: [] }, ORIGIN),
    ).toBe(false);
    expect(
      isPermissionAllowed(
        { permission: "media", requestingUrl: FROM, mediaTypes: ["unknown"] },
        ORIGIN,
      ),
    ).toBe(false);
  });

  it("서버 출처의 notifications를 허용한다", () => {
    expect(isPermissionAllowed({ permission: "notifications", requestingUrl: FROM }, ORIGIN)).toBe(
      true,
    );
  });

  it.each([
    "geolocation",
    "clipboard-read",
    "display-capture",
    "midi",
    "openExternal",
    "fullscreen",
    "",
  ])("그 밖의 권한은 거부한다: %s", (permission) => {
    expect(isPermissionAllowed({ permission, requestingUrl: FROM }, ORIGIN)).toBe(false);
  });

  it.each([
    "https://example.com/",
    "http://localhost:5173.evil.com/",
    "http://user@localhost:5173/",
    "http://localhost:5174/",
    "file:///x.html",
    "",
  ])("서버 출처가 아니면 거부한다: %s", (requestingUrl) => {
    expect(isPermissionAllowed({ permission: "notifications", requestingUrl }, ORIGIN)).toBe(false);
    expect(
      isPermissionAllowed({ permission: "media", requestingUrl, mediaTypes: ["audio"] }, ORIGIN),
    ).toBe(false);
  });
});

describe("micAccessAction", () => {
  it("macOS가 아니면 허용한다", () => {
    expect(micAccessAction("linux", "")).toBe("allow");
    expect(micAccessAction("win32", "denied")).toBe("allow");
  });

  it("macOS에서는 상태에 따라 나뉜다", () => {
    expect(micAccessAction("darwin", "granted")).toBe("allow");
    expect(micAccessAction("darwin", "not-determined")).toBe("ask");
    expect(micAccessAction("darwin", "denied")).toBe("deny");
    expect(micAccessAction("darwin", "restricted")).toBe("deny");
    expect(micAccessAction("darwin", "unknown")).toBe("ask");
  });
});
