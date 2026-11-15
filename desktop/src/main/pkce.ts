import { createHash, randomBytes } from "node:crypto";

/** base64url(패딩 없음). RFC 7636 부록 A와 같다. */
export function base64url(data: Buffer): string {
  return data.toString("base64").replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

/** `code_verifier`에서 S256 `code_challenge`를 만든다. */
export function challengeFor(verifier: string): string {
  return base64url(createHash("sha256").update(verifier, "ascii").digest());
}

/** 32바이트(256비트) 난수를 base64url로 만든 43자 값. 검증기·앱 쪽 `state`에 쓴다. */
export function randomToken(): string {
  return base64url(randomBytes(32));
}

export interface Pkce {
  verifier: string;
  challenge: string;
}

export function createPkce(): Pkce {
  const verifier = randomToken();
  return { verifier, challenge: challengeFor(verifier) };
}
