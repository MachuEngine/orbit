import { useCallback, useMemo, useRef } from "react";

/** 보안 컨텍스트가 아닌 주소(http, localhost 아님)에서는 `randomUUID`가 없어 무작위 바이트로 만든다. */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = ((b[6] ?? 0) & 0x0f) | 0x40;
  b[8] = ((b[8] ?? 0) & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/**
 * 메시지 전송의 멱등 키. 같은 내용을 다시 보낼 때(응답 유실 뒤 재시도)는 같은 키를 돌려주고,
 * 내용이 바뀌면 새 키를 만든다. 성공하거나 서버가 키를 거절하면 `discard`로 버린다.
 */
export function useIdempotencyKey() {
  const current = useRef<{ text: string; key: string } | null>(null);
  const keyFor = useCallback((text: string) => {
    if (current.current?.text !== text) current.current = { text, key: newIdempotencyKey() };
    return current.current.key;
  }, []);
  const discard = useCallback(() => {
    current.current = null;
  }, []);
  return useMemo(() => ({ keyFor, discard }), [keyFor, discard]);
}
