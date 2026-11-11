import { afterEach, describe, expect, it } from "vitest";
import { clearLoginErrorFromUrl, loginErrorMessage, readLoginError } from "./loginError";

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("loginErrorMessage", () => {
  it("알려진 종류마다 사용자 문구를 준다", () => {
    expect(loginErrorMessage("not_allowed")).toContain("허용된 계정");
    expect(loginErrorMessage("denied")).toContain("취소");
    expect(loginErrorMessage("expired")).toContain("시간이 지났");
  });

  it("모르는 값은 주소의 값을 옮기지 않고 일반 문구를 쓴다", () => {
    const text = loginErrorMessage("<img src=x>");
    expect(text).not.toContain("<img");
    expect(text).toContain("로그인하지 못했습니다");
    expect(loginErrorMessage("toString")).toBe(text);
    expect(loginErrorMessage("__proto__")).toBe(text);
  });
});

describe("login_error 쿼리", () => {
  it("값을 읽고 주소에서 지운다", () => {
    window.history.replaceState(null, "", "/?login_error=state&keep=1#top");
    expect(readLoginError(window.location.search)).toBe("state");
    clearLoginErrorFromUrl();
    expect(window.location.search).toBe("?keep=1");
    expect(window.location.hash).toBe("#top");
    expect(readLoginError(window.location.search)).toBeNull();
  });

  it("없으면 아무것도 하지 않는다", () => {
    window.history.replaceState(null, "", "/?a=1");
    clearLoginErrorFromUrl();
    expect(window.location.search).toBe("?a=1");
  });
});
