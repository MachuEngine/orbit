import { describe, expect, it } from "vitest";
import { base64url, challengeFor, createPkce, randomToken } from "./pkce";

describe("PKCE", () => {
  it("RFC 7636 부록 B의 예시 검증기에서 같은 challenge를 만든다", () => {
    expect(challengeFor("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe(
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    );
  });

  it("검증기는 32바이트 이상 난수의 base64url이고 challenge는 43자다", () => {
    const { verifier, challenge } = createPkce();
    expect(verifier).toMatch(/^[A-Za-z0-9_-]{43,128}$/);
    expect(challenge).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(challengeFor(verifier)).toBe(challenge);
    expect(verifier).not.toBe(challenge);
  });

  it("호출마다 다른 값을 만든다", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 50; i += 1) seen.add(createPkce().verifier);
    for (let i = 0; i < 50; i += 1) seen.add(randomToken());
    expect(seen.size).toBe(100);
  });

  it("base64url은 패딩과 +, /를 쓰지 않는다", () => {
    expect(base64url(Buffer.from([0xfb, 0xff, 0xfe]))).toBe("-__-");
    expect(base64url(Buffer.from([1]))).toBe("AQ");
  });
});
