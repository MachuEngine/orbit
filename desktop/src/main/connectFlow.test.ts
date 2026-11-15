import { describe, expect, it } from "vitest";
import { BEGIN_PATH, connectFlowHref } from "./connectFlow";

const ORIGIN = "http://localhost:5173";
const TICKET = "A".repeat(43);
const good = `${ORIGIN}${BEGIN_PATH}?ticket=${TICKET}`;

describe("connectFlowHref", () => {
  it("서버 출처의 desktop-begin과 티켓 하나면 정규화한 href를 돌려준다", () => {
    expect(connectFlowHref(good, ORIGIN)).toBe(good);
    expect(connectFlowHref(`HTTP://LOCALHOST:5173${BEGIN_PATH}?ticket=${TICKET}`, ORIGIN)).toBe(
      good,
    );
    expect(
      connectFlowHref(
        `https://orbit.example.test${BEGIN_PATH}?ticket=${TICKET}`,
        "https://orbit.example.test",
      ),
    ).toBe(`https://orbit.example.test${BEGIN_PATH}?ticket=${TICKET}`);
  });

  it.each([
    ["문자열이 아닌 값", 42],
    ["null", null],
    ["빈 문자열", ""],
    ["다른 호스트", `https://evil.test${BEGIN_PATH}?ticket=${TICKET}`],
    ["호스트 뒤에 도메인 붙이기", `http://localhost:5173.evil.test${BEGIN_PATH}?ticket=${TICKET}`],
    [
      "사용자 정보로 호스트 가리기",
      `http://localhost:5173@evil.test${BEGIN_PATH}?ticket=${TICKET}`,
    ],
    ["다른 포트", `http://localhost:5174${BEGIN_PATH}?ticket=${TICKET}`],
    ["다른 스킴", `https://localhost:5173${BEGIN_PATH}?ticket=${TICKET}`],
    ["javascript 스킴", "javascript:alert(1)"],
    ["file 스킴", "file:///etc/passwd"],
    ["Google 주소", "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"],
    ["다른 경로", `${ORIGIN}/api/connections/google/start?ticket=${TICKET}`],
    ["경로 뒤에 덧붙이기", `${ORIGIN}${BEGIN_PATH}/x?ticket=${TICKET}`],
    ["경로 우회(..)", `${ORIGIN}/api/../api/connections/google/desktop-begin/..?ticket=${TICKET}`],
    ["티켓 없음", `${ORIGIN}${BEGIN_PATH}`],
    ["빈 티켓", `${ORIGIN}${BEGIN_PATH}?ticket=`],
    ["짧은 티켓", `${ORIGIN}${BEGIN_PATH}?ticket=abc`],
    ["티켓에 허용하지 않는 문자", `${ORIGIN}${BEGIN_PATH}?ticket=${"A".repeat(42)}%2B`],
    ["쿼리 추가", `${ORIGIN}${BEGIN_PATH}?ticket=${TICKET}&next=https://evil.test`],
    ["티켓 중복", `${ORIGIN}${BEGIN_PATH}?ticket=${TICKET}&ticket=${TICKET}`],
    ["해시", `${ORIGIN}${BEGIN_PATH}?ticket=${TICKET}#x`],
    ["너무 긴 주소", `${ORIGIN}${BEGIN_PATH}?ticket=${"A".repeat(3000)}`],
  ])("%s는 거부한다", (_name, raw) => {
    expect(connectFlowHref(raw, ORIGIN)).toBeNull();
  });
});
