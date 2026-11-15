import type { LoopbackReceiver, LoopbackResult } from "./loopback";
import { createPkce, randomToken } from "./pkce";

/**
 * 로그인 핸드오프 진행(ADR-0024 §4). Electron에 의존하는 부분(수신기·fetch·브라우저 열기·창 다시 불러오기)은
 * 모두 주입받아, 가짜 서버로 순서를 테스트한다.
 *
 * 순서: PKCE·`state` 준비 → loopback 수신기 → 시스템 브라우저로 `/api/auth/desktop/start` →
 * 콜백(`state` 확인) → main에서 `POST /api/auth/desktop/exchange` → 세션 쿠키 저장 확인 → 창 다시 불러오기.
 * `code_verifier`는 이 모듈 밖(renderer)으로 나가지 않는다.
 */
export const START_PATH = "/api/auth/desktop/start";
export const EXCHANGE_PATH = "/api/auth/desktop/exchange";

/**
 * 실패 종류. 서버가 loopback `error=`로 알린 종류(`denied`, `not_allowed` 등)와 앱이 정한 종류
 * (`expired` 시간 초과, `state` 불일치, `exchange` 교환 실패·응답 이상, `session` 쿠키 미저장,
 * `unavailable` 서버 연결 실패)를 모두 소문자·밑줄 문자열로 보낸다.
 */
export type LoginEvent = { status: "failed"; reason: string } | { status: "done" };

export type ExchangeOutcome = "ok" | "rejected" | "unavailable";

export interface FetchInit {
  method: string;
  headers: Record<string, string>;
  body: string;
  credentials: "include";
}
export type FetchLike = (url: string, init: FetchInit) => Promise<{ status: number }>;

/** 핸드오프 코드를 세션으로 바꾼다. 응답의 세션 쿠키는 `fetch`를 만든 세션의 쿠키 저장소에 들어간다. */
export async function exchangeCode(
  fetchFn: FetchLike,
  origin: string,
  code: string,
  verifier: string,
): Promise<ExchangeOutcome> {
  try {
    const response = await fetchFn(new URL(EXCHANGE_PATH, origin).href, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, code_verifier: verifier }),
      credentials: "include",
    });
    if (response.status === 204 || response.status === 200) return "ok";
    return response.status >= 500 ? "unavailable" : "rejected";
  } catch {
    return "unavailable";
  }
}

export interface LoginDeps {
  origin: string;
  startReceiver: (options: { expectedState: string }) => Promise<LoopbackReceiver>;
  exchange: (code: string, verifier: string) => Promise<ExchangeOutcome>;
  hasSessionCookie: () => Promise<boolean>;
  openExternal: (href: string) => void;
  /** 세션이 저장된 뒤 앱 창을 다시 불러오고 앞으로 가져온다. */
  reloadWindow: () => void;
  emit: (event: LoginEvent) => void;
}

export function failureReason(result: LoopbackResult): string | null {
  switch (result.kind) {
    case "code":
    case "cancelled":
      return null;
    case "error":
      return result.reason;
    case "state_mismatch":
      return "state";
    case "invalid":
      return "exchange";
    case "timeout":
      return "expired";
  }
}

/** 로그인 시도를 하나만 진행한다. 새 시도가 시작되면 이전 시도의 수신기를 닫는다. */
export class LoginController {
  private current: LoopbackReceiver | null = null;

  constructor(private readonly deps: LoginDeps) {}

  /** 진행 중인 시도가 있으면 취소한다. 취소된 시도는 이벤트를 보내지 않는다. */
  cancel(): void {
    this.current?.cancel();
    this.current = null;
  }

  async start(): Promise<void> {
    this.cancel();
    const { deps } = this;
    const pkce = createPkce();
    const appState = randomToken();
    let receiver: LoopbackReceiver;
    try {
      receiver = await deps.startReceiver({ expectedState: appState });
    } catch {
      deps.emit({ status: "failed", reason: "unavailable" });
      return;
    }
    this.current = receiver;
    const url = new URL(START_PATH, deps.origin);
    url.searchParams.set("code_challenge", pkce.challenge);
    url.searchParams.set("port", String(receiver.port));
    url.searchParams.set("state", appState);
    deps.openExternal(url.href);

    const result = await receiver.result;
    if (this.current !== receiver) return; // 새 시도로 대체되었거나 취소되었다
    this.current = null;
    if (result.kind !== "code") {
      const reason = failureReason(result);
      if (reason !== null) deps.emit({ status: "failed", reason });
      return;
    }
    const outcome = await deps.exchange(result.code, pkce.verifier);
    if (outcome !== "ok") {
      deps.emit({
        status: "failed",
        reason: outcome === "unavailable" ? "unavailable" : "exchange",
      });
      return;
    }
    if (!(await deps.hasSessionCookie())) {
      deps.emit({ status: "failed", reason: "session" });
      return;
    }
    deps.emit({ status: "done" });
    deps.reloadWindow();
  }
}
