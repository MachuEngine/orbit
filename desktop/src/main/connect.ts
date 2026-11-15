import { failureReason } from "./login";
import type { LoopbackReceiver } from "./loopback";
import { createPkce, randomToken } from "./pkce";

/**
 * 계정 연결 완료 핸드오프(ADR-0024 §5). 브라우저는 동의만 받고, 연결 저장은 앱이 자기 세션으로 완료
 * 코드를 제출할 때만 일어난다(로그인 핸드오프와 같은 PKCE + loopback 구조).
 *
 * 순서: `prepare`(PKCE·앱 `state`·수신기 준비, `code_challenge`·포트·앱 `state`만 renderer에 준다) →
 * renderer가 티켓을 받아 `openConnectFlow`로 브라우저를 연다 → 서버가 동의 뒤
 * `http://127.0.0.1:<port>/callback?code=…&state=…`로 보낸다 → main이 `state`를 확인하고 앱 세션으로
 * `POST /api/connections/google/desktop-complete {code, code_verifier}`를 호출한다 → 결과를 이벤트로 알린다.
 * `code_verifier`는 이 모듈 밖(renderer)으로 나가지 않는다.
 */
export const COMPLETE_PATH = "/api/connections/google/desktop-complete";
export const CSRF_HEADER = "X-CSRF-Token";

export type ConnectResult = "connected" | "docs_added";

/** 실패 `reason`은 로그인과 같은 규칙이다(서버 `ConnectErrorKind`나 앱이 정한 종류, 소문자·밑줄). */
export type ConnectEvent =
  { status: "done"; result: ConnectResult } | { status: "failed"; reason: string };

export type CompleteOutcome =
  { kind: "done"; result: ConnectResult } | { kind: "failed"; reason: string };

export interface CompleteInit {
  method: string;
  headers: Record<string, string>;
  body: string;
  credentials: "include";
}
export type CompleteFetch = (
  url: string,
  init: CompleteInit,
) => Promise<{ status: number; json: () => Promise<unknown> }>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 앱 세션(쿠키)과 CSRF 헤더로 완료 API를 부른다. CSRF 쿠키 값은 호출한 쪽이 파티션에서 읽어 준다. */
export async function completeConnect(
  fetchFn: CompleteFetch,
  origin: string,
  csrfToken: string | null,
  code: string,
  verifier: string,
): Promise<CompleteOutcome> {
  if (csrfToken === null || csrfToken === "") return { kind: "failed", reason: "session" };
  let response: Awaited<ReturnType<CompleteFetch>>;
  try {
    response = await fetchFn(new URL(COMPLETE_PATH, origin).href, {
      method: "POST",
      headers: { "Content-Type": "application/json", [CSRF_HEADER]: csrfToken },
      body: JSON.stringify({ code, code_verifier: verifier }),
      credentials: "include",
    });
  } catch {
    return { kind: "failed", reason: "unavailable" };
  }
  if (response.status === 401 || response.status === 403)
    return { kind: "failed", reason: "session" };
  if (response.status >= 500) return { kind: "failed", reason: "unavailable" };
  if (response.status !== 200) return { kind: "failed", reason: "exchange" };
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { kind: "failed", reason: "exchange" };
  }
  if (!isRecord(body)) return { kind: "failed", reason: "exchange" };
  if (body.result === "connected" || body.result === "docs_added")
    return { kind: "done", result: body.result };
  if (
    body.result === "error" &&
    typeof body.error === "string" &&
    /^[a-z_]{1,32}$/.test(body.error)
  )
    return { kind: "failed", reason: body.error };
  return { kind: "failed", reason: "exchange" };
}

export interface PreparedConnect {
  code_challenge: string;
  port: number;
  app_state: string;
}

export interface ConnectDeps {
  startReceiver: (options: { expectedState: string }) => Promise<LoopbackReceiver>;
  complete: (code: string, verifier: string) => Promise<CompleteOutcome>;
  emit: (event: ConnectEvent) => void;
}

/** 연결 시도를 하나만 진행한다. 새 `prepare`가 오면 이전 시도의 수신기를 닫는다. */
export class ConnectController {
  private current: LoopbackReceiver | null = null;

  constructor(private readonly deps: ConnectDeps) {}

  cancel(): void {
    this.current?.cancel();
    this.current = null;
  }

  /** 수신기를 열고 renderer가 티켓 요청에 쓸 값을 돌려준다. 결과는 백그라운드에서 기다린다. */
  async prepare(): Promise<PreparedConnect> {
    this.cancel();
    const pkce = createPkce();
    const appState = randomToken();
    const receiver = await this.deps.startReceiver({ expectedState: appState });
    this.current = receiver;
    void this.finish(receiver, pkce.verifier);
    return { code_challenge: pkce.challenge, port: receiver.port, app_state: appState };
  }

  private async finish(receiver: LoopbackReceiver, verifier: string): Promise<void> {
    const result = await receiver.result;
    if (this.current !== receiver) return; // 새 시도로 대체되었거나 취소되었다
    this.current = null;
    if (result.kind !== "code") {
      const reason = failureReason(result);
      if (reason !== null) this.deps.emit({ status: "failed", reason });
      return;
    }
    const outcome = await this.deps.complete(result.code, verifier);
    this.deps.emit(
      outcome.kind === "done"
        ? { status: "done", result: outcome.result }
        : { status: "failed", reason: outcome.reason },
    );
  }
}
