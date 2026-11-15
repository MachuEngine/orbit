import { timingSafeEqual } from "node:crypto";
import http from "node:http";
import type { AddressInfo } from "node:net";

/**
 * 로그인 핸드오프용 일회용 수신기(ADR-0024 §4). `127.0.0.1`의 임의 포트에만 바인딩하고 `/callback`
 * 요청 하나를 받으면 응답한 뒤 바로 닫는다.
 * - 경로·메서드·`Host`가 다른 요청은 404/405/421로 거절하고 계속 기다린다(받은 요청으로 세지 않는다).
 * - `/callback`의 `state`가 다르거나 응답 형식이 올바르지 않으면 그 요청이 마지막이고 실패로 끝낸다.
 * - 제한 시간(기본 5분) 안에 오지 않으면 `timeout`으로 끝내고 닫는다.
 */
export const LOOPBACK_HOST = "127.0.0.1";
export const CALLBACK_PATH = "/callback";
export const DEFAULT_TIMEOUT_MS = 5 * 60 * 1000;

const CODE_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
const ERROR_PATTERN = /^[a-z_]{1,32}$/;

export type LoopbackResult =
  | { kind: "code"; code: string }
  /** 서버가 `error=<종류>`로 돌려준 실패. `reason`은 소문자·밑줄 종류 이름이다. */
  | { kind: "error"; reason: string }
  | { kind: "state_mismatch" }
  | { kind: "invalid" }
  | { kind: "timeout" }
  | { kind: "cancelled" };

export interface LoopbackReceiver {
  port: number;
  /** 결과는 한 번만 정해진다. 거절하지 않는다. */
  result: Promise<LoopbackResult>;
  /** 아직 결과가 없으면 `cancelled`로 끝내고 닫는다. */
  cancel: () => void;
}

function safeEqual(a: string, b: string): boolean {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

const PAGE_HEADERS = {
  "Content-Type": "text/html; charset=utf-8",
  "Cache-Control": "no-store",
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  Connection: "close",
} as const;

function page(title: string, hint: string): string {
  return (
    '<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>Orbit</title>' +
    "<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:28rem;margin:20vh auto;" +
    "padding:0 1.5rem;line-height:1.6;color:#222}h1{font-size:1.25rem}</style></head>" +
    `<body><main><h1>${title}</h1><p>${hint}</p></main></body></html>`
  );
}

const OK_PAGE = page("로그인을 마쳤습니다", "Orbit 앱으로 돌아가세요. 이 창은 닫아도 됩니다.");
const FAIL_PAGE = page("로그인하지 못했습니다", "Orbit 앱으로 돌아가 다시 시도해 주세요.");

/**
 * 수신기를 열고 `127.0.0.1`에 바인딩된 포트가 정해지면 돌려준다. `expectedState`는 앱이 만든
 * 값이고, 서버는 시작 요청의 값을 그대로 되돌려 보낸다.
 */
export function startLoopbackReceiver(options: {
  expectedState: string;
  timeoutMs?: number;
}): Promise<LoopbackReceiver> {
  const { expectedState } = options;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  return new Promise((resolveStart, rejectStart) => {
    let settle: (result: LoopbackResult) => void = () => undefined;
    const result = new Promise<LoopbackResult>((resolve) => {
      settle = resolve;
    });
    let done = false;
    let port = 0;
    let timer: NodeJS.Timeout | undefined;

    const server = http.createServer((req, res) => {
      if (done) {
        res.writeHead(410, { Connection: "close" });
        res.end();
        return;
      }
      if (req.headers.host !== `${LOOPBACK_HOST}:${String(port)}`) {
        res.writeHead(421, { Connection: "close" });
        res.end();
        return;
      }
      let url: URL;
      try {
        url = new URL(req.url ?? "/", `http://${LOOPBACK_HOST}:${String(port)}`);
      } catch {
        res.writeHead(400, { Connection: "close" });
        res.end();
        return;
      }
      if (url.pathname !== CALLBACK_PATH) {
        res.writeHead(404, { Connection: "close" });
        res.end();
        return;
      }
      if (req.method !== "GET") {
        res.writeHead(405, { Allow: "GET", Connection: "close" });
        res.end();
        return;
      }
      const params = url.searchParams;
      const state = params.get("state");
      const code = params.get("code");
      const error = params.get("error");
      let outcome: LoopbackResult;
      if (state === null || !safeEqual(state, expectedState)) outcome = { kind: "state_mismatch" };
      else if (error !== null && ERROR_PATTERN.test(error))
        outcome = { kind: "error", reason: error };
      else if (code !== null && error === null && CODE_PATTERN.test(code))
        outcome = { kind: "code", code };
      else outcome = { kind: "invalid" };
      done = true;
      const success = outcome.kind === "code";
      res.writeHead(success ? 200 : 400, PAGE_HEADERS);
      res.end(success ? OK_PAGE : FAIL_PAGE, () => {
        finish(outcome);
      });
    });

    function finish(outcome: LoopbackResult): void {
      done = true;
      if (timer !== undefined) clearTimeout(timer);
      settle(outcome);
      server.close();
      server.closeIdleConnections();
      // 응답을 보낸 연결은 `Connection: close`로 스스로 닫힌다. 남아 있으면 잠시 뒤 끊는다.
      setTimeout(() => {
        server.closeAllConnections();
      }, 1000).unref();
    }

    server.on("error", (error) => {
      if (!server.listening) rejectStart(error);
    });
    server.listen(0, LOOPBACK_HOST, () => {
      port = (server.address() as AddressInfo).port;
      timer = setTimeout(() => {
        finish({ kind: "timeout" });
      }, timeoutMs);
      resolveStart({
        port,
        result,
        cancel: () => {
          if (!done) finish({ kind: "cancelled" });
        },
      });
    });
  });
}
